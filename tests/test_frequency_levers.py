"""E22.3 (D97): frequency levers.

* cash-fit sizing (``arc.sizing.size_contracts`` with ``cash_available``): exact
  boundaries, ``cash_zero``, margin unaffected, cash shared within one run;
* the code-built ``Free slots`` line (single window and two sleeves) and the
  ``research.min_ranked`` ask; ``research_min_ranked = 0`` renders the old prompt;
* the loop's ``max_idle_with_free_slots`` (``loop_forced_free_slots``);
* registry keys ``research.min_ranked`` / ``loop.max_idle_with_free_slots`` reach
  their consumers.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from arc.account_profiles import DteSleeve
from arc.config import ArcSettings
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.pipeline.free_slots import free_slots
from arc.sizing import cash_after, size_contracts

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# A $2-wide debit at $1.00: max loss $100/contract; the gate counts
# 1.00 x 100 + $0.65 x 2 legs = $101.30 of settled cash per contract.
PER = Decimal("101.30")


def _size(cash: Decimal | None, suggestion: int = 5, **kw: Any) -> Any:
    return size_contracts(
        suggestion=suggestion,
        max_loss_per_contract=Decimal(100),
        equity=Decimal(25_000),
        cap_pct=0.10,
        cash_available=cash,
        cash_per_contract=PER if cash is not None else None,
        **kw,
    )


class TestCashFitSizing:
    def test_exact_boundary_two_contracts(self) -> None:
        r = _size(PER * 2)
        assert (r.contracts, r.code, r.cash_contracts) == (2, "cash_capped", 2)
        assert r.max_loss_total == Decimal(200)

    def test_one_cent_short_is_one(self) -> None:
        r = _size(PER * 2 - Decimal("0.01"))
        assert (r.contracts, r.code) == (1, "cash_capped")

    def test_less_than_one_contract_is_cash_zero(self) -> None:
        r = _size(PER - Decimal("0.01"))
        assert (r.contracts, r.code, r.trade) == (0, "cash_zero", False)
        assert "buying power" in r.reason and r.cash_contracts == 0

    def test_cash_not_binding_keeps_d18_code(self) -> None:
        assert _size(Decimal(10_000)).code == "ok"
        r = _size(Decimal(10_000), suggestion=50)  # cap 25 binds before cash (98)
        assert (r.contracts, r.code, r.cash_contracts) == (25, "capped", 98)

    def test_margin_profile_unaffected(self) -> None:
        r = _size(None)
        assert (r.contracts, r.code, r.cash_contracts) == (5, "ok", None)

    def test_cap_and_risk_zero_win_over_cash(self) -> None:
        assert _size(Decimal(0), suggestion=0).code == "risk_zero"
        r = size_contracts(
            suggestion=1,
            max_loss_per_contract=Decimal(3000),
            equity=Decimal(25_000),
            cap_pct=0.10,
            cash_available=Decimal(0),
            cash_per_contract=PER,
        )
        assert r.code == "cap_zero"

    def test_negative_cash_counts_as_zero(self) -> None:
        assert _size(Decimal(-5)).code == "cash_zero"

    def test_bad_cash_per_contract_is_invalid(self) -> None:
        r = size_contracts(
            suggestion=1,
            max_loss_per_contract=Decimal(100),
            equity=Decimal(25_000),
            cap_pct=0.10,
            cash_available=Decimal(500),
            cash_per_contract=Decimal(0),
        )
        assert r.code == "invalid_input"

    def test_two_proposals_share_cash(self) -> None:
        cash = PER * 3
        first = _size(cash, suggestion=2)
        assert first.contracts == 2
        cash = cash_after(cash, PER, first.contracts)
        assert cash == PER
        second = _size(cash, suggestion=2)
        assert (second.contracts, second.code) == (1, "cash_capped")
        third = _size(cash_after(cash, PER, second.contracts))
        assert third.code == "cash_zero"
        assert cash_after(Decimal(10), PER, 5) == Decimal(0)

    def test_reason_codes_labelled(self) -> None:
        from arc.pipeline.steps import _SIZING_REASONS

        assert _SIZING_REASONS["cash_capped"] is ReasonCode.SIZING_CASH_CAPPED
        assert _SIZING_REASONS["cash_zero"] is ReasonCode.SIZING_CASH_ZERO
        for code in (
            ReasonCode.SIZING_CASH_CAPPED,
            ReasonCode.SIZING_CASH_ZERO,
            ReasonCode.RESEARCH_UNDER_RANKED,
            ReasonCode.LOOP_FORCED_FREE_SLOTS,
        ):
            assert REASON_LABELS[code]
        assert set(REASON_LABELS) == set(ReasonCode)


def test_cash_per_contract_matches_the_gate_rule() -> None:
    """Sizing's per-contract cash equals the gate's need for one contract (D25)."""
    from arc.gate.band import PriceBand
    from arc.pipeline.steps import cash_fit_enabled, cash_per_contract, fit_basis

    class _Leg:
        ratio = 1

    class _St:
        legs = (_Leg(), _Leg())

    s = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
    band = PriceBand(lo=Decimal("0.95"), hi=Decimal("1.00"), max_steps=1)
    fee = Decimal(str(s.gate_fee_per_leg_contract)) * 2
    assert cash_per_contract(_St(), band, s) == Decimal(100) + fee
    assert cash_fit_enabled(s)
    # D129: margin profiles fit too, to options buying power (band-worst max loss + fees)
    assert cash_fit_enabled(s.with_profile("margin"))
    assert (
        fit_basis(s) == "settled_cash"
        and fit_basis(s.with_profile("margin")) == "options_buying_power"
    )


def test_buying_power_per_contract_matches_the_gate_rule() -> None:
    """D129: sizing's per-contract need on margin equals the gate's need for one contract."""
    from arc.gate.band import PriceBand
    from arc.pipeline.steps import buying_power_per_contract, fit_per_contract

    class _Leg:
        ratio = 1

    class _Credit:  # bull put 5 wide for a 0.85 credit: max loss $415 per unit
        legs = (_Leg(), _Leg())
        max_loss = Decimal("415")
        net_debit_credit = Decimal("-0.85")

    class _Unbounded:
        legs = (_Leg(),)
        max_loss = None
        net_debit_credit = Decimal("1.00")

    s = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    fee = Decimal(str(s.gate_fee_per_leg_contract)) * 2
    # the band's worst price is a smaller credit (-0.80): max loss grows by $5
    band = PriceBand(lo=Decimal("-0.85"), hi=Decimal("-0.80"), max_steps=1)
    assert buying_power_per_contract(_Credit(), band, s) == Decimal("420") + fee
    assert fit_per_contract(_Credit(), band, s) == Decimal("420") + fee
    assert buying_power_per_contract(_Unbounded(), band, s) is None
    assert fit_per_contract(_Unbounded(), band, s) is None


# ---------------------------------------------------------------------------
# Free slots
# ---------------------------------------------------------------------------


def _sl(name: str, lo: int, hi: int, cap: int | None) -> DteSleeve:
    return DteSleeve(name=name, dte_min=lo, dte_max=hi, max_positions=cap, exit_policy=None)


SHORT, SWING = _sl("short", 10, 29, 5), _sl("swing", 30, 60, 5)
LINE_TWO = "Free slots: short 3/5, swing 1/5 \u00b7 settled cash $18,250 \u00b7 per-name cap $2,500"


class TestFreeSlots:
    def test_two_sleeves_line(self) -> None:
        fs = free_slots(
            [SHORT, SWING],
            [12, 20, 45, 50, 33, 40],
            max_open_positions=10,
            single=False,
            per_name_cap=Decimal(2500),
            settled_cash=Decimal("18250.40"),
            opens_today={"swing": 1},
        )
        assert fs.line() == LINE_TWO
        assert fs.total_free == 4 and fs.pressure()  # short: no open today
        assert fs.asked(4, 17) == 4 and fs.asked(4, 3) == 3 and fs.asked(10, 17) == 8

    def test_single_window_line(self) -> None:
        fs = free_slots(
            [_sl("default", 30, 60, None)],
            [35, 40, None],
            max_open_positions=8,
            single=True,
            per_name_cap=Decimal(10_000),
        )
        assert fs.line() == "Free slots: 5/8 \u00b7 per-name cap $10,000"
        assert fs.total_free == 5

    def test_margin_line_shows_options_buying_power(self) -> None:
        """D129: margin profiles show Alpaca's options buying power, not settled cash."""
        fs = free_slots(
            [_sl("default", 30, 60, None)],
            [35],
            max_open_positions=10,
            single=True,
            per_name_cap=Decimal(2354),
            options_buying_power=Decimal("20065.37"),
        )
        assert fs.line() == (
            "Free slots: 9/10 \u00b7 buying power $20,065 \u00b7 per-name cap $2,354"
        )
        # unknown buying power: no figure rather than a wrong one
        plain = free_slots(
            [_sl("default", 30, 60, None)],
            [35],
            max_open_positions=10,
            single=True,
            per_name_cap=Decimal(2354),
        )
        assert plain.line() == "Free slots: 9/10 \u00b7 per-name cap $2,354"

    def test_global_cap_bounds_each_sleeve(self) -> None:
        fs = free_slots(
            [SHORT, SWING],
            [12] * 4 + [40] * 5,
            max_open_positions=10,
            single=False,
            per_name_cap=Decimal(2500),
        )
        assert [s.free for s in fs.sleeves] == [1, 0]

    def test_unknown_entry_dte_fills_every_sleeve(self) -> None:
        fs = free_slots(
            [SHORT, SWING], [None], max_open_positions=10, single=False, per_name_cap=Decimal(1)
        )
        assert [s.free for s in fs.sleeves] == [4, 4]

    def test_pressure_needs_free_slot_and_a_quiet_sleeve(self) -> None:
        kw: dict[str, Any] = {"max_open_positions": 10, "single": False, "per_name_cap": Decimal(1)}
        busy = free_slots([SHORT, SWING], [], opens_today={"short": 1, "swing": 2}, **kw)
        assert not busy.pressure()
        full = free_slots([SHORT, SWING], [12] * 5 + [40] * 5, **kw)
        assert full.total_free == 0 and not full.pressure() and full.asked(4, 10) == 0

    def test_no_sleeves_renders_zero(self) -> None:
        fs = free_slots([], [], max_open_positions=8, single=True, per_name_cap=Decimal(1))
        assert fs.line().startswith("Free slots: 0/0") and fs.total_free == 0


# ---------------------------------------------------------------------------
# Research prompt (golden lines + rollback)
# ---------------------------------------------------------------------------


def _research_prompt(**kw: Any) -> str:
    from arc.context.store import ContextSnapshot
    from arc.personas.builders import build_research_prompt, research_input_from_context

    snap = ContextSnapshot(id="snap-1", as_of=dt.datetime(2026, 10, 12, tzinfo=dt.UTC), entries=[])
    return build_research_prompt(
        research_input_from_context(
            snap,
            portfolio_summary="Equity $25,000.00. 0 open option position(s).",
            scan_date="2026-10-12",
            compact=True,
            pool_merged=True,
            **kw,
        )
    )


SECTION_TWO = (
    "\n### Free slots (code-counted; open positions by entry-DTE sleeve, E22.3)\n"
    "Free slots: short 3/5, swing 1/5 \u00b7 settled cash $18,250 \u00b7 per-name cap $2,500\n"
    "Rank at least 4 names when the pool has that many that pass your own bar, spread "
    "across the sleeves with free slots; settled cash is first come within a loop, in your "
    "rank order. If you rank fewer, say why in one line in `session_notes`. This never "
    "forces a trade: an idea that fails your bar stays out.\n"
)
SECTION_ONE = (
    "\n### Free slots (code-counted; open positions by entry-DTE sleeve, E22.3)\n"
    "Free slots: 5/8 \u00b7 per-name cap $10,000\n"
    "Rank at least 4 names when the pool has that many that pass your own bar. If you rank "
    "fewer, say why in one line in `session_notes`. This never forces a trade: an idea that "
    "fails your bar stays out.\n"
)


class TestResearchPrompt:
    def test_two_sleeve_golden(self) -> None:
        p = _research_prompt(free_slots=LINE_TWO, min_ranked=4)
        assert SECTION_TWO + "\n## Prior notes" in p

    def test_single_window_golden(self) -> None:
        p = _research_prompt(free_slots="Free slots: 5/8 \u00b7 per-name cap $10,000", min_ranked=4)
        assert SECTION_ONE + "\n## Prior notes" in p

    def test_no_ask_when_nothing_to_ask(self) -> None:
        p = _research_prompt(free_slots="Free slots: 0/8 \u00b7 per-name cap $10,000", min_ranked=0)
        assert "Free slots: 0/8" in p and "Rank at least" not in p

    def test_unset_is_the_old_prompt(self) -> None:
        assert "Free slots" not in _research_prompt()
        assert _research_prompt() == _research_prompt(free_slots="", min_ranked=0)


def _fixture_run(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> tuple[Any, Any, dict[str, str]]:
    from tests.test_pipeline import _recording_fixture_run

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    from arc.routines.config import load_routines
    from tests.legacy_profiles import legacy_margin_settings

    s = legacy_margin_settings(**kw)  # pre-D128 single-window margin: "Free slots: 10/10"
    return _recording_fixture_run(s, load_routines())


class TestResearchStep:
    def test_min_ranked_zero_renders_old_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rollback: research_min_ranked=0 -> no free-slots inputs, prompt as before E22.3."""
        conn, report, prompts = _fixture_run(monkeypatch, research_min_ranked=0)
        assert not report.failed
        assert "Free slots" not in prompts["research"]
        (raw,) = conn.execute(
            "SELECT prompt_inputs FROM persona_calls WHERE persona='research'"
        ).fetchone()
        inputs = json.loads(raw)
        assert "free_slots" not in inputs and "min_ranked" not in inputs
        n = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE reason_code='research_under_ranked'"
        ).fetchone()[0]
        assert n == 0

    def test_free_slots_line_and_under_ranked_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn, report, prompts = _fixture_run(monkeypatch)
        assert not report.failed
        assert re.search(
            r"\nFree slots: 10/10 \u00b7 buying power \$100,000 \u00b7 per-name cap \$[\d,]+"
            r"\nRank at least 4 names when",
            prompts["research"],
        )
        (row,) = conn.execute(
            "SELECT choice, reason_text, payload FROM decisions "
            "WHERE reason_code='research_under_ranked'"
        ).fetchall()
        payload = json.loads(row["payload"])
        assert row["choice"] == "noted"
        assert payload["asked"] == 4 and payload["ranked"] < 4 and payload["min_ranked"] == 4
        assert row["reason_text"].startswith(f"ranked {payload['ranked']} of 4 asked")

    def test_replay_rebuilds_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hashlib

        from arc.context.store import ContextStore
        from arc.pipeline.steps import build_prompt

        conn, report, _ = _fixture_run(monkeypatch)
        assert not report.failed
        row = conn.execute(
            "SELECT snapshot_id, prompt_inputs, prompt_sha256 FROM persona_calls "
            "WHERE persona='research'"
        ).fetchone()
        snap = ContextStore(conn).load_snapshot(row["snapshot_id"])
        prompt = build_prompt("research", snap, json.loads(row["prompt_inputs"]))
        assert hashlib.sha256(prompt.encode()).hexdigest() == row["prompt_sha256"]

    def test_cash_profile_line_carries_settled_cash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
        from arc.routines.config import load_routines
        from tests.test_pipeline import _recording_fixture_run

        s = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
        _, report, prompts = _recording_fixture_run(s, load_routines())
        assert not report.failed
        assert re.search(
            # E22.4 (D97): the shipped cash profile names its two sleeves
            r"\nFree slots: short 5/5, swing 5/5 \u00b7 settled cash \$[\d,]+ \u00b7 "
            r"per-name cap \$[\d,]+\n",
            prompts["research"],
        )


def test_propose_cash_capped_and_cash_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """quant.propose fits the cash-account open to settled cash (fewer contracts / none)."""
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline import FIXTURE_NOW, PipelineEnv
    from arc.pipeline.env import FIXTURE_SETS
    from arc.pipeline.runner import open_db, run_propose
    from arc.routines.config import load_routines
    from arc.routines.heartbeat import LogNotifier

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)

    def run(cash: str, profile: str = "cash_debit") -> sqlite3.Connection:
        s = ArcSettings(  # type: ignore[call-arg]
            _env_file=None, account_profile=profile, portfolio_dollar_delta_cap_pct=1.0
        )
        s._yaml_overrides = {"ranking": {("ranking", "filters", "live"): False}}  # noqa: SLF001
        from tests.legacy_profiles import pin_control_menu

        pin_control_menu(s)  # D128: the canned Quant reply is on the control menu
        env = PipelineEnv.fixtures(FIXTURE_SETS["bullish"])
        base = env.account()
        env.account = lambda: base.model_copy(update={"cash": Decimal(cash)})  # type: ignore[method-assign]
        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        run_propose(
            conn, s, load_routines(), env, now=FIXTURE_NOW, notifier=LogNotifier(), mode="fixtures"
        )
        return conn

    def sizing(conn: sqlite3.Connection) -> tuple[str, str, dict[str, Any]]:
        (row,) = conn.execute(
            "SELECT reason_code, reason_text, payload FROM decisions WHERE stage='sizing'"
        ).fetchall()
        return row[0], row[1], json.loads(row[2])

    code, text, payload = sizing(run("3000"))
    per = Decimal(payload["cash_per_contract"])
    assert code == "sizing:cash_capped" and payload["contracts"] == int(Decimal(3000) // per)
    assert ", cash " in text and payload["cash_available"] == "3000"
    gate = run("3000").execute("SELECT reason_code FROM decisions WHERE stage='gate'").fetchall()
    assert [r[0] for r in gate] == ["gate:pass"]  # sizing no longer proposes a cash fail

    code, text, payload = sizing(run(str(per - Decimal("0.01"))))
    assert code == "sizing:cash_zero" and payload["contracts"] == 0

    rows = (
        run("3000", "margin")  # margin: no cash fit
        .execute("SELECT reason_code, payload FROM decisions WHERE stage='sizing'")
        .fetchall()
    )
    assert all(
        r[0] not in ("sizing:cash_capped", "sizing:cash_zero")
        and "cash_available" not in json.loads(r[1] or "{}")
        for r in rows
    )


def test_swap_open_passes_cash_fit() -> None:
    """The swap open path (``arc/positions/steps.py``) sizes with the same fit (D129)."""
    import inspect

    from arc.positions import steps

    src = inspect.getsource(steps._open_leg)  # noqa: SLF001
    assert "fit_per_contract(st, band, settings) if cash_fit_enabled(settings)" in src
    assert "cash_available=fit_available(account, settings) if per_fit is not None" in src


# ---------------------------------------------------------------------------
# Loop: max_idle_with_free_slots
# ---------------------------------------------------------------------------


def test_loop_settings_default_and_yaml() -> None:
    from arc.routines.config import LoopSettings, load_routines

    assert LoopSettings().max_idle_with_free_slots == dt.timedelta(minutes=20)
    assert load_routines().loop.max_idle_with_free_slots == dt.timedelta(minutes=20)
    with pytest.raises(ValueError, match="must be positive"):
        LoopSettings(max_idle_with_free_slots="0m")  # type: ignore[arg-type]


class TestLoopFreeSlots:
    """The same fixture loop as tests/test_routines_e58.py, with free-slot pressure."""

    def _disp(self, conn: sqlite3.Connection, routines: Any) -> Any:
        from tests.test_routines_e58 import _disp

        return _disp(conn, routines)

    def test_forced_full_run_after_free_slots_idle(self) -> None:
        from arc.routines.config import load_routines
        from arc.routines.loop import LoopState
        from tests.test_routines_e58 import SLOT0, _conn, _llm_calls, _seed_scalp, _slot, _warm

        routines = load_routines(
            overrides={("loop", "max_idle"): "60m", ("loop", "max_idle_with_free_slots"): "20m"}
        )
        conn = _conn()
        _seed_scalp(conn, routines)
        disp = self._disp(conn, routines)
        _warm(disp)
        quiet = _slot(disp, SLOT0 + dt.timedelta(minutes=10))
        assert quiet["research"].metrics["no_change"] is True
        before = _llm_calls(conn)
        late = SLOT0 + dt.timedelta(minutes=26)  # 21m after the last full run
        out = _slot(disp, late)
        assert out["research"].metrics["no_change"] is False
        assert _llm_calls(conn) > before
        assert LoopState(conn).last_full_run() == late
        (row,) = conn.execute(
            "SELECT choice, reason_text, payload FROM decisions "
            "WHERE reason_code='loop_forced_free_slots'"
        ).fetchall()
        # D128: the default margin profile carries the two D97 sleeves
        assert row["choice"] == "noted"
        assert "Free slots: short 5/5, swing 5/5" in row["reason_text"]
        assert [s["free"] for s in json.loads(row["payload"])["free_slots"]["sleeves"]] == [5, 5]

    def test_no_pressure_keeps_max_idle(self) -> None:
        """Every slot taken: the plain max_idle applies (no forced run)."""
        from arc.routines.config import load_routines
        from tests.test_routines_e58 import SLOT0, _conn, _seed_scalp, _slot, _warm

        routines = load_routines(
            overrides={("loop", "max_idle"): "60m", ("loop", "max_idle_with_free_slots"): "20m"}
        )
        conn = _conn()
        _seed_scalp(conn, routines)
        disp = self._disp(conn, routines)
        _warm(disp)
        import arc.pipeline.steps as steps_mod
        from arc.pipeline.free_slots import FreeSlots, SleeveFree

        full = FreeSlots(
            sleeves=[SleeveFree(name="default", free=0, cap=8, opens_today=0)],
            single=True,
            per_name_cap=Decimal(1),
        )
        mp = pytest.MonkeyPatch()
        mp.setattr(steps_mod, "_free_slots", lambda ctx, env: full)
        try:
            out = _slot(disp, SLOT0 + dt.timedelta(minutes=26))
        finally:
            mp.undo()
        assert out["research"].metrics["no_change"] is True
        n = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE reason_code='loop_forced_free_slots'"
        ).fetchone()[0]
        assert n == 0


# ---------------------------------------------------------------------------
# Registry → consumer
# ---------------------------------------------------------------------------


OWNER = "U0OWNER001"


def _svc(conn: sqlite3.Connection) -> Any:
    from arc.control.service import ControlService

    base = ArcSettings(_env_file=None, approver_slack_user_ids=[OWNER])  # type: ignore[call-arg]
    now = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.UTC)
    return ControlService(
        conn, base=base, now=lambda: now, optionable=lambda s: True, is_halted=lambda: False
    )


@pytest.fixture
def conn() -> sqlite3.Connection:
    from arc.store.db import connect
    from arc.store.migrate import migrate

    c = connect(":memory:")
    migrate(c)
    return c


def _set(svc: Any, key: str, value: str) -> None:
    r = svc.set(key, value, actor=OWNER, source="slack")
    if r.pending is not None:
        svc.confirm(r.pending.code, actor=OWNER, source="slack")
    else:
        assert r.outcome == "applied", r


def test_registry_min_ranked_reaches_settings(conn: sqlite3.Connection) -> None:
    from arc.control.effective import effective_settings

    svc = _svc(conn)
    assert svc.view("research.min_ranked").value == 4
    _set(svc, "research.min_ranked", "6")
    assert effective_settings(conn).research_min_ranked == 6
    _set(svc, "research.min_ranked", "0")  # the rollback
    assert effective_settings(conn).research_min_ranked == 0
    assert svc.set("research.min_ranked", "11", actor=OWNER, source="slack").outcome == "refused"


def test_registry_free_slots_idle_reaches_routines(conn: sqlite3.Connection) -> None:
    from arc.control.effective import effective_routines

    svc = _svc(conn)
    assert svc.view("loop.max_idle_with_free_slots").value == 20
    _set(svc, "loop.max_idle_with_free_slots", "30")
    assert effective_routines(conn).loop.max_idle_with_free_slots == dt.timedelta(minutes=30)
    assert svc.view("loop.max_idle_with_free_slots").value == 30


def test_settings_default(tmp_path: Path) -> None:
    assert ArcSettings(_env_file=None).research_min_ranked == 4  # type: ignore[call-arg]
