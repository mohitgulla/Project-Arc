"""E22.2 / D97: quant.open builds one menu per (ticker, open DTE sleeve).

Fixture chains (as of 2026-09-25) carry two expiries, 35 and 42 DTE. The tests switch
the margin profile to two sleeves that split them (``near`` 30-39, ``far`` 40-60) so
both sleeves get menu items from the same chain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from arc.account_profiles import DteSleeve, sleeve_slots
from arc.config import ArcSettings
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.models import Proposal
from arc.routines.config import load_routines
from arc.scanner.scan import ScanParams

if TYPE_CHECKING:
    import pytest

CHAINS_MARK = "### Option chains with Greeks\n"

SLEEVES = {
    "near": {"dte_min": 30, "dte_max": 39, "max_positions": 5},
    "far": {"dte_min": 40, "dte_max": 60, "max_positions": 5},
}


def _profiles(tmp_path: Path, sleeves: dict[str, Any], profile: str = "margin") -> Path:
    data = yaml.safe_load(Path("config/account_profiles.yaml").read_text())
    p = data["profiles"][profile]
    p.pop("dte_min", None)
    p.pop("dte_max", None)
    p["sleeves"] = sleeves
    f = tmp_path / "account_profiles.yaml"
    f.write_text(yaml.safe_dump(data))
    return f


def _settings(tmp_path: Path, sleeves: dict[str, Any] | None = None) -> ArcSettings:
    return ArcSettings(  # type: ignore[call-arg]
        _env_file=None,
        account_profile="margin",
        account_profiles_file=_profiles(tmp_path, SLEEVES if sleeves is None else sleeves),
    )


def _run(settings: ArcSettings, monkeypatch: pytest.MonkeyPatch) -> Any:
    from tests import test_pipeline as tp

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    return tp._recording_fixture_run(settings, load_routines())


def _chains(prompt: str) -> dict[str, Any]:
    return json.loads(prompt.split(CHAINS_MARK, 1)[1].split("\n\n### ", 1)[0])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _sl(name: str, lo: int, hi: int, cap: int | None) -> DteSleeve:
    return DteSleeve(name=name, dte_min=lo, dte_max=hi, max_positions=cap, exit_policy=None)


class TestSleeveSlots:
    def test_counts_by_entry_dte(self) -> None:
        short, swing = _sl("short", 10, 29, 2), _sl("swing", 30, 60, None)
        out = sleeve_slots([short, swing], [12, 20, 45])
        assert [(s.held, s.free, s.open) for s in out] == [(2, 0, False), (1, None, True)]
        assert [s.text() for s in out] == ["short 2/2", "swing 1"]

    def test_unknown_entry_dte_counts_toward_every_sleeve(self) -> None:
        out = sleeve_slots([_sl("a", 10, 29, 1), _sl("b", 30, 60, 3)], [None])
        assert [(s.held, s.open) for s in out] == [(1, False), (1, True)]

    def test_out_of_window_position_counts_nowhere(self) -> None:
        out = sleeve_slots([_sl("a", 10, 29, 1)], [90])
        assert out[0].held == 0 and out[0].free == 1

    def test_zero_cap_is_closed(self) -> None:
        (s,) = sleeve_slots([_sl("a", 10, 29, 0)], [])
        assert s.free == 0 and not s.open


def test_scan_params_for_one_sleeve(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    far = s.entry_sleeves[1]
    p = ScanParams.from_settings(s, sleeve=far)
    assert (p.dte_min, p.dte_max, p.dte_windows) == (40, 60, None)
    env = ScanParams.from_settings(s)
    assert (env.dte_min, env.dte_max, env.dte_windows) == (30, 60, [(30, 39), (40, 60)])


def test_reason_label() -> None:
    assert REASON_LABELS[ReasonCode.SLEEVE_FULL] == "No menu: DTE sleeve full"
    assert set(REASON_LABELS) == set(ReasonCode)


# ---------------------------------------------------------------------------
# Fixture pipeline
# ---------------------------------------------------------------------------


class TestPerSleeveMenus:
    def test_menu_split_and_cut_per_sleeve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s = _settings(tmp_path)
        conn, report, prompts = _run(s, monkeypatch)
        assert not report.failed
        chains = _chains(prompts["quant"])
        rows = [r for c in chains.values() for r in c["menu"]]
        assert {r["sleeve"] for r in rows} == {"near", "far"}  # both sleeves got items
        for menu in (c["menu"] for c in chains.values()):
            by: dict[str, list[dict[str, Any]]] = {}
            for row in menu:
                by.setdefault(row["sleeve"], []).append(row)
            for name, mine in by.items():
                assert len(mine) <= s.pipeline_scan_top  # each sleeve cut on its own
                lo, hi = {"near": (30, 39), "far": (40, 60)}[name]
                assert all(lo <= r["dte"] <= hi for r in mine)
        # NVDA's bull puts sit in both expiries: one menu item per sleeve
        assert {r["sleeve"] for r in chains["NVDA"]["menu"]} == {"near", "far"}
        # prompt carries the sleeve slots + the sleeve-aware entry terms
        assert "DTE sleeve slots (open positions/max): near 0/5, far 0/5." in prompts["quant"]
        assert "Entry windows: near 30-39 DTE, far 40-60 DTE" in prompts["quant"]
        assert "Entry windows: near 30-39 DTE, far 40-60 DTE" in prompts["risk"]
        # no sleeve is full -> no sleeve_full journal rows
        n = conn.execute("select count(*) from decisions where reason_code='sleeve_full'")
        assert n.fetchone()[0] == 0

    def test_proposal_carries_sleeve(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from arc.approvals.card import render_card
        from arc.approvals.trail import load_trail
        from arc.gate.rules import proposal_hash
        from arc.journal.store import JournalStore
        from arc.models import GateDecision

        conn, report, _ = _run(_settings(tmp_path), monkeypatch)
        (row,) = report.proposals
        st = json.loads(row["structure_json"])
        assert st["sleeve"] in {"near", "far"}
        raw = conn.execute("SELECT payload FROM context_entries WHERE kind='proposal'").fetchone()
        p = Proposal.model_validate_json(raw[0])
        assert p.structure.sleeve == st["sleeve"]
        mc = JournalStore(conn).market_context(row["proposal_hash"])
        assert mc is not None and mc.analytics is not None
        window = {"near": "30\u201339 DTE", "far": "40\u201360 DTE"}[st["sleeve"]]
        assert mc.analytics.sleeve_window == window
        ph = proposal_hash(p)
        view = render_card(
            p,
            GateDecision(proposal_hash=ph, passed=True, token="t"),
            proposal_hash=ph,
            actionable=True,
            trail=load_trail(conn, row["proposal_hash"], "SPY"),
        )
        assert f"*Sleeve*\\n{st['sleeve']} ({window})" in json.dumps(
            view.blocks, ensure_ascii=False
        )
        # quant.propose summary counts opens per sleeve (0s included)
        (summary,) = conn.execute(
            "select summary from routine_runs where job='quant.propose'"
        ).fetchone()
        other = "far" if st["sleeve"] == "near" else "near"
        assert f"{st['sleeve']} 1" in summary and f"{other} 0" in summary
        assert "per sleeve: near" in summary

    def test_full_sleeve_gets_no_menu_and_is_journaled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeves = {**SLEEVES, "near": {**SLEEVES["near"], "max_positions": 0}}
        conn, report, prompts = _run(_settings(tmp_path, sleeves), monkeypatch)
        assert not report.failed
        chains = _chains(prompts["quant"])
        rows = [r for c in chains.values() for r in c["menu"]]
        assert rows and {r["sleeve"] for r in rows} == {"far"}
        assert "near 0/0" in prompts["quant"]
        full = conn.execute(
            "select subject, payload from decisions where reason_code='sleeve_full'"
        ).fetchall()
        assert {r[0] for r in full} == set(chains)
        assert all(json.loads(r[1])["sleeve"] == "near" for r in full)

    def test_every_sleeve_full_no_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeves = {k: {**v, "max_positions": 0} for k, v in SLEEVES.items()}
        conn, report, _ = _run(_settings(tmp_path, sleeves), monkeypatch)
        assert not report.failed
        assert report.proposals == []
        rows = conn.execute(
            "select reason_text from decisions where reason_code='sleeve_full'"
        ).fetchall()
        assert rows and all("every DTE sleeve is full" in r[0] for r in rows)
        assert (
            conn.execute("select count(*) from decisions where reason_code='no_chain'").fetchone()[
                0
            ]
            == 0
        )


def test_single_window_menu_has_no_sleeve(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.legacy_profiles import legacy_margin_settings

    s = legacy_margin_settings()  # D128: shipped margin has sleeves; pin the single window
    _, report, prompts = _run(s, monkeypatch)
    for c in _chains(prompts["quant"]).values():
        assert all("sleeve" not in r for r in c["menu"])
    assert "DTE sleeve slots" not in prompts["quant"]
    (row,) = report.proposals
    assert "sleeve" not in json.loads(row["structure_json"])


# ---------------------------------------------------------------------------
# Card acceptance shape: D97 sleeves on cash_debit, chains at 14/21/35/49 DTE
# ---------------------------------------------------------------------------


def _redated_market(dtes: tuple[int, ...]) -> Any:
    """The fixture recordings with each chain's front expiry copied to every DTE in *dtes*."""
    import datetime as dt

    from arc.data.recorded import MULTI_NAME_FIXTURES, RecordedMarketData, load_recording
    from arc.pipeline.env import FIXTURE_NOW
    from arc.structures import format_occ

    as_of = FIXTURE_NOW.date()
    recs = []
    for path in MULTI_NAME_FIXTURES:
        rec = load_recording(path)
        front = min(c.expiration for c in rec.contracts)
        base = [c for c in rec.contracts if c.expiration == front]
        contracts = []
        for d in dtes:
            exp = as_of + dt.timedelta(days=d)
            for c in base:
                sym = format_occ(rec.underlying, exp, c.option_type, c.strike)
                contracts.append(c.model_copy(update={"expiration": exp, "symbol": sym}))
        recs.append(rec.model_copy(update={"contracts": contracts}))
    return RecordedMarketData(*recs)


def _d97_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeves: dict[str, Any]) -> Any:
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline import PipelineEnv
    from arc.pipeline.env import FIXTURE_NOW
    from arc.pipeline.runner import open_db, run_propose
    from arc.routines.heartbeat import LogNotifier

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    s = ArcSettings(  # type: ignore[call-arg]
        _env_file=None,
        account_profile="cash_debit",
        account_profiles_file=_profiles(tmp_path, sleeves, "cash_debit"),
    )
    env = PipelineEnv.fixtures()
    env.market = _redated_market((14, 21, 35, 49))
    prompts: dict[str, str] = {}

    class _Rec:
        def __init__(self, persona: str, inner: Any) -> None:
            self.persona, self.inner = persona, inner
            self.model = getattr(inner, "model", "fixture")

        def complete(self, prompt: str) -> Any:
            prompts[self.persona] = prompt
            return self.inner.complete(prompt)

    env.llms = {k: _Rec(k, v) for k, v in env.llms.items()}  # type: ignore[misc]
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    report = run_propose(
        conn, s, load_routines(), env, now=FIXTURE_NOW, notifier=LogNotifier(), mode="fixtures"
    )
    return conn, report, prompts


D97 = {
    "short": {"dte_min": 10, "dte_max": 29, "max_positions": 5},
    "swing": {"dte_min": 30, "dte_max": 60, "max_positions": 5},
}


class TestD97Acceptance:
    def test_both_sleeves_get_menu_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, report, prompts = _d97_run(tmp_path, monkeypatch, D97)
        assert not report.failed
        rows = [r for c in _chains(prompts["quant"]).values() for r in c["menu"]]
        dtes = {"short": {14, 21}, "swing": {35, 49}}
        assert {r["sleeve"] for r in rows} == {"short", "swing"}
        for r in rows:
            assert r["dte"] in dtes[r["sleeve"]]
        assert "Entry windows: short 10-29 DTE, swing 30-60 DTE" in prompts["quant"]

    def test_short_full_only_swing_and_journal_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeves = {**D97, "short": {**D97["short"], "max_positions": 0}}
        conn, report, prompts = _d97_run(tmp_path, monkeypatch, sleeves)
        assert not report.failed
        chains = _chains(prompts["quant"])
        rows = [r for c in chains.values() for r in c["menu"]]
        assert rows and {r["sleeve"] for r in rows} == {"swing"}
        full = conn.execute(
            "select subject, choice, payload from decisions where reason_code='sleeve_full'"
        ).fetchall()
        assert {r[0] for r in full} == set(chains)
        assert all(r[1] == "no_trade" and json.loads(r[2])["sleeve"] == "short" for r in full)


# ---------------------------------------------------------------------------
# Dedupe and the per-underlying cap across sleeves (D33 / D97)
# ---------------------------------------------------------------------------


def test_swing_open_does_not_suppress_short_idea() -> None:
    import datetime as dt
    from decimal import Decimal

    from arc.pipeline.dedupe import (
        DedupeConfig,
        RecentIdea,
        Suppression,
        check_idea,
        fingerprint,
    )
    from arc.structures import long_call
    from arc.utils.calendar import ET

    as_of = dt.date(2026, 10, 12)
    now = dt.datetime(2026, 10, 12, 10, 0, tzinfo=ET)
    swing = long_call("SPY", as_of + dt.timedelta(days=45), 580, "6.00", as_of=as_of)
    short = long_call("SPY", as_of + dt.timedelta(days=17), 580, "4.00", as_of=as_of)
    fp_swing = fingerprint("SPY", "bullish", swing, Decimal("580"))
    fp_short = fingerprint("SPY", "bullish", short, Decimal("580"))
    assert fp_swing.key() != fp_short.key()  # expiry_week separates the sleeves
    held = RecentIdea(
        fingerprint=fp_swing.key(),
        kind=Suppression.EXECUTED,
        at=now,
        spot=Decimal("580"),
        ref="s1",
        open_structure=True,
    )
    cfg = DedupeConfig()
    assert not check_idea(fp_short, [held], spot=580, regime=None, cfg=cfg).suppressed
    assert check_idea(fp_swing, [held], spot=580, regime=None, cfg=cfg).suppressed


def test_per_underlying_cap_binds_across_sleeves(tmp_path: Path) -> None:
    """A swing position's max loss counts against a short open on the same ticker."""
    import datetime as dt
    from decimal import Decimal

    from arc.gate import Portfolio
    from arc.gate.inputs import Position
    from arc.structures import long_call
    from tests.test_gate_sleeves import AS_OF, codes, gate

    s = ArcSettings(  # type: ignore[call-arg]
        _env_file=None,
        account_profile="cash_debit",
        account_profiles_file=_profiles(tmp_path, D97, "cash_debit"),
    )
    short = long_call("SPY", AS_OF + dt.timedelta(days=17), 570, "6.00", as_of=AS_OF)
    # gate() uses $100k equity -> 10% = $10,000 per underlying; this one costs $600
    swing_spy = Portfolio(
        positions=[Position(underlying="SPY", max_loss=Decimal("9600"), entry_dte=45)]
    )
    assert "per_underlying_limit" in codes(gate(short, s, swing_spy))
    swing_other = Portfolio(
        positions=[Position(underlying="QQQ", max_loss=Decimal("9600"), entry_dte=45)]
    )
    assert "per_underlying_limit" not in codes(gate(short, s, swing_other))
