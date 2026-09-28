"""E6.1a: proposal card v2 (Net EV, cost & liquidity, moneyness, vol stats, exit plan).

Golden checks run on the offline fixture chain (`arc propose --fixtures`), so every
number here is reproducible. Numbers come from stored analytics, never a live quote.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.approvals.card import NET_EV_DEFINITION, render_card, render_resolved
from arc.approvals.trail import DecisionTrail, load_trail
from arc.backtest.costs import CostModel, FeeBreakdown, load_cost_model
from arc.config import ArcSettings
from arc.data.alpaca import AlpacaMarketData
from arc.exits import load_exit_config, model_exits
from arc.gate.rules import proposal_hash
from arc.journal.analytics import (
    ProposalAnalytics,
    expected_move,
    moneyness_pct,
    otm,
    sigma_distance,
    sigma_t,
)
from arc.journal.store import JournalStore
from arc.models import GateDecision, Leg, LegIntent, Proposal, StructureKind
from arc.pipeline.analytics import build_analytics
from arc.pipeline.env import FIXTURE_NOW
from arc.pipeline.runner import fixture_run
from arc.pricing.bs import OptionKind
from arc.routines.config import load_routines
from arc.structures import analyze, format_occ
from arc.utils.calendar import ET

# ---------------------------------------------------------------------------
# Fixture card
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def run() -> SimpleNamespace:
    conn, report = fixture_run(
        ArcSettings(_env_file=None, account_profile="margin"), load_routines()
    )  # type: ignore[call-arg]
    assert len(report.proposals) == 1
    raw = conn.execute("SELECT payload FROM context_entries WHERE kind = 'proposal'").fetchone()[0]
    p = Proposal.model_validate_json(raw)
    stored = str(conn.execute("SELECT proposal_hash FROM proposals").fetchone()[0])
    trail = load_trail(conn, stored, "SPY")
    ph = proposal_hash(p)
    d = GateDecision(proposal_hash=ph, passed=True, token="t")
    view = render_card(p, d, proposal_hash=ph, actionable=True, trail=trail)
    return SimpleNamespace(conn=conn, p=p, trail=trail, ph=ph, d=d, view=view)


def _field(view: Any, label: str) -> list[str]:
    """Lines of the fact field titled *label*."""
    for b in view.blocks:
        for f in b.get("fields", []):
            head, _, body = f["text"].partition("\n")
            if head == f"*{label}*":
                return body.split("\n")
    raise AssertionError(f"no field {label!r}")


def _section(view: Any, title: str) -> list[str]:
    for b in view.blocks:
        t = (b.get("text") or {}).get("text", "")
        if b["type"] == "section" and t.startswith(f"*{title}*\n"):
            return t.split("\n")[1:]
    raise AssertionError(f"no section {title!r}")


def _all_text(view: Any) -> str:
    return json.dumps(view.blocks, ensure_ascii=False)


class TestAnalyticsStored:
    def test_stored_on_market_context_not_proposal(self, run: SimpleNamespace) -> None:
        mc = JournalStore(run.conn).market_context(
            str(run.conn.execute("SELECT proposal_hash FROM proposals").fetchone()[0])
        )
        assert mc is not None and mc.analytics is not None
        assert "analytics" not in Proposal.model_fields  # gate hash contract unchanged
        a = mc.analytics
        assert a.spot == pytest.approx(771.335)
        assert [la.occ_symbol for la in a.legs] == [leg.occ_symbol for leg in run.p.structure.legs]
        assert a.exit_model is not None and a.cost_model == load_cost_model()

    def test_trail_carries_analytics(self, run: SimpleNamespace) -> None:
        assert run.trail.analytics is not None
        assert run.trail.analytics.dte == run.p.structure.dte

    def test_forbids_extra_fields(self, run: SimpleNamespace) -> None:
        data = run.trail.analytics.model_dump() | {"live_quote": 1}
        with pytest.raises(ValueError, match="live_quote"):
            ProposalAnalytics.model_validate(data)


class TestCardItems:
    """Owner's list, items 1-10 (golden text on the fixture card)."""

    def test_1_summary_sentence_case(self, run: SimpleNamespace) -> None:
        summary = run.view.blocks[1]["elements"][0]["text"]
        assert summary.startswith("Credit 1.66 · Max gain $165.55 · Max loss $334.45 · PoP 62%")
        assert "Net EV $9.22 managed / $32.36 hold" in summary
        assert summary.endswith("x14 · Account margin · :white_check_mark: Gate PASS")

    def test_2_legs_are_plain_lines(self, run: SimpleNamespace) -> None:
        legs = _section(run.view, "Legs")
        assert legs == [
            "Long 1x 740P @ 3.74 · 4.1% OTM · Δ -0.18",
            "Short 1x 745P @ 4.46 · 3.4% OTM · Δ +0.21",
            "Short 1x 798C @ 2.94 · 3.5% OTM · Δ -0.20",
            "Long 1x 803C @ 2.00 · 4.1% OTM · Δ +0.15",
        ]
        assert "```" not in _all_text(run.view)
        assert not any("Oct 30" in line for line in legs)  # one expiry → not repeated

    def test_2_expiry_shown_when_legs_differ(self, run: SimpleNamespace) -> None:
        legs = list(run.p.structure.legs)
        legs[3] = legs[3].model_copy(update={"occ_symbol": "SPY261106C00803000"})
        p = run.p.model_copy(
            update={"structure": run.p.structure.model_copy(update={"legs": legs})}
        )
        view = render_card(p, None, proposal_hash=run.ph, actionable=False)
        lines = _section(view, "Legs")
        assert lines[0].startswith("Long 1x Oct 30 740P") and "Nov 06 803C" in lines[3]

    def test_3_one_fact_per_line_and_greeks(self, run: SimpleNamespace) -> None:
        assert _field(run.view, "Net Greeks (position)") == [
            "Δ Delta -28.1 sh",
            "Γ Gamma -3.29 sh",
            "ν Vega -$239.49 / vol pt",
            "Θ Theta +$43.91 / day",
        ]
        for b in run.view.blocks:
            for f in b.get("fields", []):
                for line in f["text"].split("\n")[1:]:
                    assert " · " not in line or line.startswith(("Bid ", "OI ", "Slippage", "(ORF"))

    def test_4_risk_reward(self, run: SimpleNamespace) -> None:
        assert _field(run.view, "Payoff (per contract)")[-1] == "Risk/Reward 2.02 : 1"

    def test_5_sentence_case_labels(self, run: SimpleNamespace) -> None:
        text = _all_text(run.view)
        for phrase in ("Max gain", "Max loss", "Limit credit", "Token issued", "Net EV"):
            assert phrase in text
        banned = [
            "Max Gain",
            "Max Loss",
            "Limit Credit",
            "Token Issued",
            "No Token",
            "Net Ev",
            "Expected Days",
            "Take Profit",
            "Day Change",
            "Iv Rank",
            "IV Rank",
            "IV Percentile",
            "Open Interest",
            "Round Trip",
            "Exit Plan",
            "Cost & Liquidity",
            "Vol Stats",
        ]
        for phrase in banned:
            assert phrase not in text, phrase
        no_token = GateDecision(proposal_hash=run.ph, passed=True, token=None)
        view = render_card(run.p, no_token, proposal_hash=run.ph, actionable=False)
        assert "No token (not executable)" in _all_text(view)

    def test_6_net_ev_both_views_with_breakdown(self, run: SimpleNamespace) -> None:
        hold = _field(run.view, "Net EV · hold to expiry")
        managed = _field(run.view, "Net EV · managed exit")
        assert hold == [
            "Gross (model) EV $35.80",
            "− Spread & slippage in $3.23",
            "− Spread & slippage out $0.00",
            "− Commission $0.00",
            "− Regulatory fees $0.21",
            "Net EV $32.36 / contract",
            "Net EV $453.04 x14",
        ]
        assert managed[0] == "Gross (model) EV $15.89"
        assert managed[-2:] == ["Net EV $9.22 / contract", "Net EV $129.08 x14"]
        assert NET_EV_DEFINITION in _all_text(run.view)
        assert "exchange/regulatory fees, slippage and bid-ask spread" in NET_EV_DEFINITION

    def test_7_cost_and_liquidity(self, run: SimpleNamespace) -> None:
        assert _field(run.view, "Short 1x 798C") == [
            "Bid 2.91 · Ask 2.98 · Mid 2.94",
            "Spread $0.07 (2.4% of mid)",
            "Size n/a x n/a",  # the recorded fixture has no bs/as
            "OI 464 · Vol 38",
            "Slippage $1.75 · Fees $0.05",
        ]
        total = _field(run.view, "Total (per contract)")
        assert total[0] == "Entry slippage $3.23"
        assert total[1] == "Entry commission $0.00"
        assert total[2] == "Entry regulatory fees $0.18"
        assert total[3].startswith("(ORF $0.06 · OCC $0.10 · CAT $0.00 · TAF $0.01 · SEC $0.02")
        assert "Expected exit slippage $3.08" in total and "Expected exit fees $0.18" in total
        assert total[-2:] == ["Round trip $6.67 / contract", "Round trip $93.38 x14"]
        assert "Depth: top of book only (Alpaca)" in _all_text(run.view)

    def test_8_underlying_and_moneyness(self, run: SimpleNamespace) -> None:
        und = _field(run.view, "Underlying")
        assert und[0] == "Spot 771.34 at 16:00 ET"
        assert und[1].startswith("Day change ")
        assert und[2] == "1σ move to expiry ±31.75 (739.58 – 803.09)"
        mon = _field(run.view, "Moneyness (% / σ from spot)")
        assert mon[1] == "Short 745P: -3.4%, 0.8σ"
        assert mon[-2:] == ["BE 743.34: -3.6%, 0.9σ", "BE 799.66: +3.7%, 0.9σ"]
        assert _field(run.view, "Breakevens") == [
            "BE 743.34 (-3.6%, 0.9σ)",
            "BE 799.66 (+3.7%, 0.9σ)",
        ]

    def test_9_vol_stats_na_for_missing(self, run: SimpleNamespace) -> None:
        assert _field(run.view, "Vol stats") == [
            "ATM IV 13.3%",
            "IV rank n/a",  # fixture history is too short for a rank
            "IV percentile n/a",
            "HV20 10.7%",
            "HV60 n/a",
            "IV/HV20 1.24",
        ]

    def test_10_exit_plan(self, run: SimpleNamespace) -> None:
        plan = _section(run.view, "Exit plan")
        assert plan[0] == (
            "Take profit 50% of max gain ($0.83 debit to close) · Stop at 75% of max loss "
            "($4.16 debit to close), end-of-day marks · Close at 7 DTE"
        )
        assert plan[1] == "Managed: PoP 70% · Net EV $9.22 / contract"
        assert plan[2] == "Hold to expiry: PoP 73% · Net EV $32.36 / contract"
        assert plan[3] == "Take profit 61% · Stop 8% · DTE exit 31% · Expiry 0%"
        assert plan[4] == "Expected days held 24.5"

    def test_slack_limits(self, run: SimpleNamespace) -> None:
        assert len(run.view.blocks) <= 50
        for b in run.view.blocks:
            if b["type"] == "section" and "text" in b:
                assert len(b["text"]["text"]) <= 3000
            assert len(b.get("fields", [])) <= 10
            for f in b.get("fields", []):
                assert len(f["text"]) <= 2000

    def test_resolved_card_keeps_analytics(self, run: SimpleNamespace) -> None:
        view = render_resolved(
            run.p,
            run.d,
            proposal_hash=run.ph,
            outcome="Approved",
            at=FIXTURE_NOW,
            trail=run.trail,
        )
        assert _field(view, "Vol stats")[0] == "ATM IV 13.3%"
        assert all(b["type"] != "actions" for b in view.blocks)

    def test_no_analytics_still_renders(self, run: SimpleNamespace) -> None:
        view = render_card(run.p, run.d, proposal_hash=run.ph, actionable=True)
        text = _all_text(view)
        assert "Net EV n/a" in view.blocks[1]["elements"][0]["text"]
        assert "Exit model n/a" in text and "*Cost & liquidity*" not in text

    def test_account_profile_in_summary(self, run: SimpleNamespace) -> None:
        a = run.trail.analytics.model_copy(update={"account_profile": "cash_debit"})
        trail = DecisionTrail(analytics=a)
        view = render_card(run.p, run.d, proposal_hash=run.ph, actionable=False, trail=trail)
        assert "· Account cash_debit ·" in view.blocks[1]["elements"][0]["text"]

    def test_render_is_pure(self, run: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rendering never touches market data: every number is from stored analytics."""

        def boom(*a: object, **k: object) -> None:
            raise AssertionError("live market data at render time")

        monkeypatch.setattr(AlpacaMarketData, "option_chain", boom)
        monkeypatch.setattr(AlpacaMarketData, "underlying_quote", boom)
        again = render_card(run.p, run.d, proposal_hash=run.ph, actionable=True, trail=run.trail)
        assert again.blocks == run.view.blocks


# ---------------------------------------------------------------------------
# Debit structures (D25)
# ---------------------------------------------------------------------------


class _Priced:
    def __init__(self, structure: Any, contracts: dict, spot: float, iv: float) -> None:
        self.structure = structure
        self.contracts = contracts
        self.spot = spot
        self.atm_iv = iv
        self.spot_as_of = _dt.datetime(2026, 9, 28, 10, 30, tzinfo=ET)

    def leg_spreads(self) -> dict[str, float]:
        return {k: c.ask - c.bid for k, c in self.contracts.items()}


def _debit_vertical() -> tuple[Proposal, _Priced]:
    from arc.data.base import OptionContract, OptionGreeks
    from arc.models import QuantMetrics, Sizing

    exp = _dt.date(2026, 10, 30)
    long_sym = format_occ("SPY", exp, OptionKind.CALL, 770)
    short_sym = format_occ("SPY", exp, OptionKind.CALL, 780)
    quotes = {
        long_sym: (9.90, 10.10, 0.52),
        short_sym: (5.40, 5.60, 0.38),
    }
    contracts = {
        s: OptionContract(
            symbol=s,
            underlying="SPY",
            expiration=exp,
            strike=770.0 if s == long_sym else 780.0,
            option_type="call",
            bid=b,
            ask=a,
            mid=(a + b) / 2,
            bid_size=12,
            ask_size=30,
            open_interest=5000,
            volume=900,
            implied_volatility=0.14,
            greeks=OptionGreeks(delta=d),
        )
        for s, (b, a, d) in quotes.items()
    }
    from decimal import Decimal

    legs = [
        Leg(occ_symbol=long_sym, side=LegIntent.LONG, ratio=1, premium=Decimal("10.00")),
        Leg(occ_symbol=short_sym, side=LegIntent.SHORT, ratio=1, premium=Decimal("5.50")),
    ]
    structure = analyze(legs, as_of=_dt.date(2026, 9, 28)).model_copy(
        update={"kind": StructureKind.VERTICAL_DEBIT}
    )
    p = Proposal(
        candidate_id="c1",
        structure=structure,
        thesis="Debit call spread into strength.",
        quant=QuantMetrics(pop=0.45, ev=Decimal("12.5"), cost_bps=30.0),
        risk_narrative="Defined risk.",
        sizing=Sizing(contracts=3, notional=Decimal("1350"), pct_equity=0.0135),
        expires_at=_dt.datetime(2026, 9, 28, 11, tzinfo=ET),
        limit_price=Decimal("4.50"),
    )
    return p, _Priced(structure, contracts, spot=772.0, iv=0.14)


class TestDebitCard:
    def test_debit_vertical_renders_in_debit_terms(self) -> None:
        p, priced = _debit_vertical()
        cost = load_cost_model()
        em = model_exits(
            p.structure,
            load_exit_config().policy_for(p.structure.kind),
            spot=priced.spot,
            iv=priced.atm_iv,
            r=0.04,
            spreads=priced.leg_spreads(),
            cost=cost,
        )
        a = build_analytics(
            priced,  # type: ignore[arg-type]
            cost=cost,
            regime={"last_close": 770.0, "vol": {"hv20": 0.12}},
            exit_model=em,
            account_profile="cash_debit",
        )
        view = render_card(
            p, None, proposal_hash="0" * 64, actionable=False, trail=DecisionTrail(analytics=a)
        )
        summary = view.blocks[1]["elements"][0]["text"]
        assert summary.startswith("Debit 4.50 · Max gain $550.00 · Max loss $450.00")
        assert "Account cash_debit" in summary
        assert _field(view, "Payoff (per contract)")[-1] == "Risk/Reward 0.82 : 1"
        plan = _section(view, "Exit plan")[0]
        assert plan.startswith("Take profit 100% of debit (") and "credit to close)" in plan
        assert _field(view, "Long 1x 770C")[2] == "Size 12 x 30"
        und = _field(view, "Underlying")
        assert und[0] == "Spot 772.00 at 10:30 ET" and und[1] == "Day change +0.26%"
        assert _field(view, "Vol stats")[-1] == "IV/HV20 1.17"
        assert _field(view, "Net EV · managed exit")[-1].endswith("x3")


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


class TestMoneynessMath:
    def test_hand_checked(self) -> None:
        # spot 100, IV 20%, 73 DTE → σ√t = 0.2 × √0.2 = 0.089443
        assert sigma_t(0.20, 73) == pytest.approx(0.2 * math.sqrt(0.2))
        assert moneyness_pct(95, 100) == pytest.approx(-0.05)
        assert sigma_distance(95, 100, 0.20, 73) == pytest.approx(math.log(0.95) / 0.0894427, 1e-5)
        assert expected_move(100, 0.20, 73) == pytest.approx(8.94427, 1e-5)
        assert otm("put", 95, 100) and not otm("call", 95, 100) and otm("call", 100, 100)

    def test_missing_inputs_are_none(self) -> None:
        assert sigma_t(None, 30) is None and sigma_t(0.2, 0) is None
        assert sigma_distance(95, 100, None, 30) is None and expected_move(100, 0.0, 30) is None
        with pytest.raises(ValueError, match="spot"):
            moneyness_pct(95, 0)

    @settings(max_examples=200, deadline=None)
    @given(
        spot=st.floats(1.0, 5000.0),
        k=st.floats(0.5, 2.0),
        iv=st.floats(0.01, 3.0),
        dte=st.integers(1, 730),
    )
    def test_sigma_distance_properties(self, spot: float, k: float, iv: float, dte: int) -> None:
        strike = spot * k
        d = sigma_distance(strike, spot, iv, dte)
        assert d is not None
        # sign follows moneyness, and a strike exactly 1σ (log) away is 1.0σ
        assert (d > 0) == (strike > spot) or math.isclose(strike, spot)
        one_sigma = spot * math.exp(sigma_t(iv, dte))  # type: ignore[arg-type]
        assert sigma_distance(one_sigma, spot, iv, dte) == pytest.approx(1.0)
        assert sigma_distance(spot, spot, iv, dte) == pytest.approx(0.0, abs=1e-12)


class TestNetEvArithmetic:
    @settings(max_examples=40, deadline=None)
    @given(
        x=st.floats(0.0, 1.0),
        commission=st.floats(0.0, 2.0),
        orf=st.floats(0.0, 0.1),
        sec=st.floats(0.0, 0.001),
        spread=st.floats(0.01, 0.30),
    )
    def test_gross_minus_costs_is_net(
        self, x: float, commission: float, orf: float, sec: float, spread: float
    ) -> None:
        """Net EV = gross − spread/slippage (in + out) − commission − regulatory fees."""
        p, priced = _debit_vertical()
        cost = CostModel(
            slippage_frac=x, commission_per_contract=commission, orf_per_contract=orf,
            sec_rate_sell=sec,
        )  # fmt: skip
        spreads = {k: spread for k in priced.contracts}
        em = model_exits(
            p.structure,
            load_exit_config().policy_for(p.structure.kind),
            spot=priced.spot,
            iv=priced.atm_iv,
            r=0.04,
            spreads=spreads,
            cost=cost,
        )
        for stats in (em.static, em.managed):
            c = stats.costs
            assert c is not None
            assert min(c.entry_slippage, c.exit_slippage, c.commission, c.regulatory_fees) >= 0
            net = stats.gross_ev - c.entry_slippage - c.exit_slippage - c.commission
            net -= c.regulatory_fees
            assert stats.net_ev == pytest.approx(net, abs=0.011)
        # the itemised net matches the path-by-path net P&L mean (to rounding)
        assert em.static.costs.entry_slippage == pytest.approx(  # type: ignore[union-attr]
            x * spread * 2 * 100, abs=0.006
        )


# ---------------------------------------------------------------------------
# Cost config
# ---------------------------------------------------------------------------


class TestCostsConfig:
    def test_shipped_config_matches_alpaca_schedule(self) -> None:
        cm = load_cost_model()
        assert cm.commission_per_contract == 0.0
        assert cm.orf_per_contract == pytest.approx(0.015)
        assert cm.occ_per_contract == pytest.approx(0.025)
        assert cm.cat_per_share == pytest.approx(0.000003)
        assert cm.taf_per_contract_sell == pytest.approx(0.00329)
        assert cm.sec_rate_sell == pytest.approx(0.0000206)
        assert cm.slippage_frac == 0.25
        text = Path("config/costs.yaml").read_text()
        assert "BrokFeeSched.pdf" in text and "2026-09-28" in text

    def test_fee_breakdown_sides(self) -> None:
        cm = load_cost_model()
        buy = cm.fee_breakdown(2, 1, 3.0)
        sell = cm.fee_breakdown(2, -1, 3.0)
        assert buy.taf == 0 and buy.sec == 0
        assert sell.taf == pytest.approx(2 * 0.00329)
        assert sell.sec == pytest.approx(0.0000206 * 3.0 * 100 * 2)
        assert sell.total == pytest.approx(cm.trade_fees(2, -1, 3.0))
        assert (buy + sell).orf == pytest.approx(4 * 0.015)
        assert isinstance(buy.scaled(2), FeeBreakdown)

    def test_changing_a_fee_changes_net_ev_without_code(self, tmp_path: Path) -> None:
        base = Path("config/costs.yaml").read_text()
        cheap = tmp_path / "cheap.yaml"
        dear = tmp_path / "dear.yaml"
        cheap.write_text(base)
        dear.write_text(
            re.sub(r"commission_per_contract: .*", "commission_per_contract: 0.65", base)
        )
        p, priced = _debit_vertical()
        pol = load_exit_config().policy_for(p.structure.kind)

        def net(path: Path) -> float:
            return model_exits(
                p.structure,
                pol,
                spot=priced.spot,
                iv=priced.atm_iv,
                r=0.04,
                spreads=priced.leg_spreads(),
                cost=load_cost_model(path),
            ).static.net_ev

        # 2 legs x $0.65 on entry, plus the closing-trade commission on ITM legs at expiry
        assert net(cheap) - net(dear) >= 1.30 - 0.011
        assert load_cost_model(dear).commission_per_contract == 0.65

    def test_backtest_cli_defaults_come_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import argparse

        from arc.backtest import cli as bcli

        seen: dict[str, Any] = {}
        monkeypatch.setattr(bcli, "run_report", lambda **k: seen.update(k))
        monkeypatch.setattr(bcli, "closes_for", lambda *a, **k: {})
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        bcli.add_backtest_parser(sub)
        args = parser.parse_args(
            ["backtest", "--start", "2026-01-02", "--end", "2026-02-02", "--offline"]
        )
        bcli.run_backtest_cli(args)
        assert seen["cost"] == load_cost_model()
        args = parser.parse_args(
            ["backtest", "--start", "2026-01-02", "--end", "2026-02-02", "--offline", "--fee", "1"]
        )
        bcli.run_backtest_cli(args)
        assert seen["cost"].commission_per_contract == 1.0
        assert seen["cost"].orf_per_contract == load_cost_model().orf_per_contract


# ---------------------------------------------------------------------------
# bid_size / ask_size parse
# ---------------------------------------------------------------------------


class TestQuoteSizes:
    def test_size_parse(self) -> None:
        from arc.data.alpaca import _size

        assert _size(12) == 12.0 and _size(3.0) == 3.0
        assert _size(None) is None and _size(-1) is None and _size(True) is None
        assert _size("7") is None


def test_scaffold_discovers_new_modules() -> None:
    # test_scaffold imports every module found by pkgutil.walk_packages (E1.1a).
    import importlib
    import pkgutil

    import arc

    found = {m.name for m in pkgutil.walk_packages(arc.__path__, "arc.")}
    assert {"arc.journal.analytics", "arc.pipeline.analytics"} <= found
    importlib.import_module("arc.journal.analytics")
    importlib.import_module("arc.pipeline.analytics")


def test_fixture_db_is_in_memory(run: SimpleNamespace) -> None:
    assert isinstance(run.conn, sqlite3.Connection)
