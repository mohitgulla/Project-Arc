"""E5.2 pipeline runner: sizing (D18), step filters, gate wiring, `arc propose --fixtures`."""

from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.ingest.llm import FixtureScoutLLM, LLMResult, ScoutLLMError
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
    return ArcSettings(_env_file=None)  # type: ignore[call-arg]


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
        snap = account_snapshot(info, FIXTURE_NOW)
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
    def test_end_to_end(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        conn, report = fixture_run(settings, routines)
        assert [(o.job, o.status) for o in report.outcomes] == [
            ("scout", "ok"),
            ("director", "ok"),
            ("quant", "ok"),
            ("risk", "ok"),
            ("propose", "ok"),
        ]
        assert not report.failed
        # every chain step (director → propose) shares one chain_run_id
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
        assert calls["director"]["status"] == "ok"
        assert json.loads(calls["director"]["dropped"]) == {"not_a_candidate": 2}

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
        # later the same day: steps run, propose skips the existing (day, ticker)
        later = FIXTURE_NOW + _dt.timedelta(minutes=5)
        conn, report = fixture_run(settings, routines, db=db, now=later)
        assert len(report.proposals) == 1
        propose = next(o for o in report.outcomes if o.job == "propose")
        assert "already proposed" in (propose.summary or "")
        assert len(proposals_for_day(conn, "2026-09-25")) == 1

    def test_fixtures_never_mint_token_even_with_secret(
        self,
        routines,
        monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    ) -> None:
        monkeypatch.setenv("ARC_GATE_SECRET", GATE_SECRET)
        _, report = fixture_run(ArcSettings(_env_file=None), routines)  # type: ignore[call-arg]
        (p,) = report.proposals
        assert p["gate_passed"]
        assert not p["gate_token"]  # dry-run / fixtures: the gate verdict only, no permission

    def test_gate_token_minted_when_env_allows(
        self,
        routines,
        monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    ) -> None:
        from arc.ingest.scout import load_fixture_docs
        from arc.pipeline.runner import run_propose
        from arc.routines.heartbeat import LogNotifier

        monkeypatch.setenv("ARC_GATE_SECRET", GATE_SECRET)
        settings = ArcSettings(_env_file=None)  # type: ignore[call-arg]
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
        raise ScoutLLMError("provider down")


class TestDigestCards:
    """E5.5: each persona step posts a digest card; the fallback one-liner is unchanged."""

    def test_fixture_chain_posts_a_card_per_persona(
        self,
        settings: ArcSettings,
        routines,  # noqa: ANN001
    ) -> None:
        from arc.ingest.scout import load_fixture_docs
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
            "[Scout] Scan: 10 docs → 3 candidates",
            "[Director] Shortlist: 1 of 3 • market risk_on",
            "[Quant] Structures: SPY Iron Condor • PoP 62% • EV -$21.78",
            "[Risk] Review: SPY moderate • suggests 20",
            None,  # propose has no card (E6.1 posts the proposal card)
        ]
        # Fallback text = the pre-E5.5 one-liners.
        assert texts[0] == "[Scout] scout ✓ 10 docs → 5 accepted, 3 candidates today"
        assert texts[1] == (
            "[Director] director ✓ 3 candidates → shortlist: SPY (neutral); "
            "dropped {'not_a_candidate': 2}"
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
        scout = "\n".join(
            b["text"]["text"] for b in notes.blocks[0] or [] if b["type"] == "section"
        )
        assert "• not in universe (1): PLTR" in scout
        director = json.dumps(notes.blocks[1])
        assert "not a Scout candidate (2): AAPL, PLTR" in director
        assert "not picked by Director (2): NVDA, XOM" in director
        risk = json.dumps(notes.blocks[3])
        assert "structure Quant did not propose (1): QQQ iron_condor" in risk


class TestFailures:
    def _env(self, **llms: object) -> PipelineEnv:
        env = PipelineEnv.fixtures()
        env.llms.update(llms)  # type: ignore[arg-type]
        return env

    def _run(self, settings: ArcSettings, routines, env: PipelineEnv):  # noqa: ANN001, ANN202
        from arc.ingest.scout import load_fixture_docs
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
        assert status["director"] == "ok"
        assert status["quant"] == "failed"
        assert "risk" not in status and "propose" not in status
        assert report.failed
        assert not report.proposals
        rows = conn.execute("SELECT status FROM persona_calls WHERE persona='quant'").fetchall()
        assert [r["status"] for r in rows] == ["llm_error"]

    def test_unparseable_reply_fails(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        _, report = self._run(
            settings, routines, self._env(director=FixtureScoutLLM(["not json at all"]))
        )
        assert {o.job: o.status for o in report.outcomes}["director"] == "failed"

    def test_risk_declines(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        risk = json.loads((FIXTURES_DIR / "risk.json").read_text())
        risk["assessments"][0]["sizing_suggestion"] = 0
        _, report = self._run(
            settings, routines, self._env(risk=FixtureScoutLLM([json.dumps(risk)]))
        )
        propose = next(o for o in report.outcomes if o.job == "propose")
        assert propose.status == "ok"
        assert "suggested 0" in (propose.summary or "")
        assert not report.proposals

    def test_empty_shortlist(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        d = {"shortlist": [], "market_regime": "risk_off", "session_notes": "nothing"}
        _, report = self._run(
            settings, routines, self._env(director=FixtureScoutLLM([json.dumps(d)]))
        )
        assert all(o.status == "ok" for o in report.outcomes)
        assert not report.proposals


def test_cli_propose_fixtures(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:  # noqa: ANN001
    from arc.cli import main

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    assert main(["propose", "--fixtures", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "fixtures"
    assert [p["ticker"] for p in out["proposals"]] == ["SPY"]


def test_cli_live_propose_requires_gate_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.config
    from arc.cli import main

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    monkeypatch.setattr(arc.config, "get_settings", lambda: ArcSettings(_env_file=None))  # type: ignore[call-arg]

    def _no_live_env(*_a: object, **_k: object) -> None:
        raise AssertionError("must refuse before touching Alpaca/Hermes")

    monkeypatch.setattr(PipelineEnv, "live", _no_live_env)
    assert main(["propose", "--no-scout"]) == 2
    assert "ARC_GATE_SECRET" in capsys.readouterr().err


def test_cli_dry_run_does_not_require_gate_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    import arc.config
    from arc.cli import main

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    monkeypatch.setattr(arc.config, "get_settings", lambda: ArcSettings(_env_file=None))  # type: ignore[call-arg]
    assert main(["propose", "--fixtures", "--json"]) == 0
