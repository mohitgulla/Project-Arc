"""E18.2 (D78): the fill-day guard: no discretionary close on the fill day unless the
thesis is broken.

Pure truth table over :func:`arc.positions.exit_case.fill_day_filter` /
:func:`fill_day_guarded`, the config + registry switch, and the fixture research
chain (bundled SPY recording + fixture personas; ``FIXTURE_NOW`` is Fri 2026-09-25
16:00 ET).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest

from arc.exits.policy import PositionsConfig, load_exit_config
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.pipeline import FIXTURE_NOW
from arc.positions.evaluate import SignalKind
from arc.positions.exit_case import (
    MANDATORY_KINDS,
    ExitTrigger,
    case_skip_reason,
    fill_day_filter,
    fill_day_guarded,
    opened_session,
    triggers_for,
)
from arc.utils.calendar import ET
from tests.test_exit_case import _review, _watch
from tests.test_research_exit_watch import _codes, _kind, _routines, _run
from tests.test_risk_exit import _closes, _personas, _settings, _verdict

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings
    from arc.pipeline import PipelineEnv

FRI = dt.date(2026, 9, 25)
MON = dt.date(2026, 9, 28)
OPENED_FRI = "2026-09-25T14:05:00+00:00"  # 10:05 ET

STATUSES = ("intact", "weakened", "broken")
TRIGGER_KINDS = ("review", "swap", "stop", "profit_lock", "take_profit", "dte", "ev_floor")


# ---------------------------------------------------------------------------
# pure: which trigger the guard holds
# ---------------------------------------------------------------------------


def _trig(kind: str) -> ExitTrigger:
    return ExitTrigger(kind=kind, detail=f"{kind} fired")  # type: ignore[arg-type]


#: The card's truth table: (trigger, thesis) -> held on a guarded day.
#: Mandatory signals never become triggers (no exit case): exits.mandatory closes
#: them whatever the guard says, which ``test_mandatory_signals_never_reach_the_guard``
#: pins.
_SIGNAL_OF = {
    "stop": SignalKind.STOP,
    "profit_lock": SignalKind.PROFIT_LOCK,
    "take_profit": SignalKind.PROFIT_TARGET,
    "dte": SignalKind.DTE_EXIT,
    "ev_floor": SignalKind.REMAINING_EV_FLOOR,
}


def _expected_held(kind: str, status: str) -> bool | None:
    """``True`` held, ``False`` kept, ``None`` never a case (mandatory)."""
    if kind == "review":
        return status != "broken"
    if kind in ("swap", "ev_floor"):
        return True
    if kind == "take_profit":
        return False
    return None  # stop / profit_lock / dte: mandatory


class TestTruthTable:
    @pytest.mark.parametrize("status", STATUSES)
    @pytest.mark.parametrize("kind", TRIGGER_KINDS)
    def test_fill_day(self, kind: str, status: str) -> None:
        sig = _SIGNAL_OF.get(kind)
        rv = _review("os1", sig) if sig is not None else _review("os1")
        watch = _watch("review", status) if kind == "review" else None
        expected = _expected_held(kind, status)
        if expected is None:  # mandatory: never a case, the guard never sees it
            assert sig in MANDATORY_KINDS
            assert case_skip_reason(rv) == "mandatory_pending"
            return
        trig = triggers_for(rv, watch)
        if kind == "swap":
            trig = [_trig("reallocate")]
        assert trig, kind
        kept, held = fill_day_filter(trig, status if watch is not None else None)
        assert (held == trig) is expected
        assert (kept == trig) is (not expected)

    def test_time_adjusted_target_is_kept(self) -> None:
        kept, held = fill_day_filter([_trig("time_adjusted_target")], None)
        assert held == [] and len(kept) == 1

    def test_mixed_triggers_split(self) -> None:
        trig = [_trig("research_review"), _trig("profit_target"), _trig("remaining_ev_floor")]
        kept, held = fill_day_filter(trig, "weakened")
        assert [t.kind for t in kept] == ["profit_target"]
        assert [t.kind for t in held] == ["research_review", "remaining_ev_floor"]
        kept, held = fill_day_filter(trig, "broken")
        assert [t.kind for t in kept] == ["research_review", "profit_target"]

    def test_mandatory_signals_never_reach_the_guard(self) -> None:
        for sig in MANDATORY_KINDS:
            assert case_skip_reason(_review("os1", sig)) == "mandatory_pending"

    def test_review_without_watch_item_is_held(self) -> None:
        kept, held = fill_day_filter([_trig("research_review")], None)
        assert kept == [] and len(held) == 1


class TestGuardedWindow:
    def test_fill_day_only_by_default(self) -> None:
        assert fill_day_guarded(OPENED_FRI, FRI, 1) is True
        assert fill_day_guarded(OPENED_FRI, MON, 1) is False  # day+1 passes through

    def test_sessions_count_trading_days(self) -> None:
        assert fill_day_guarded(OPENED_FRI, MON, 2) is True  # Fri + the next session
        assert fill_day_guarded(OPENED_FRI, dt.date(2026, 9, 29), 2) is False
        assert fill_day_guarded(OPENED_FRI, dt.date(2026, 9, 29), 3) is True

    def test_off_and_unknown(self) -> None:
        assert fill_day_guarded(OPENED_FRI, FRI, 0) is False
        assert fill_day_guarded(None, FRI, 1) is False
        assert fill_day_guarded("not a time", FRI, 1) is False
        assert fill_day_guarded(OPENED_FRI, dt.date(2026, 9, 24), 1) is False

    def test_et_date_of_the_fill(self) -> None:
        # 2026-09-26 02:00 UTC is Fri 22:00 ET: the fill day is Friday
        assert opened_session("2026-09-26T02:00:00+00:00") == FRI
        assert opened_session("2026-09-26T02:00:00") == FRI  # naive = UTC
        local = dt.datetime(2026, 9, 25, 9, 45, tzinfo=ET).isoformat()
        assert opened_session(local) == FRI


# ---------------------------------------------------------------------------
# config + registry
# ---------------------------------------------------------------------------


class TestConfig:
    def test_shipped_default_on(self) -> None:
        cfg = load_exit_config().positions
        assert cfg.fill_day_guard is True and cfg.fill_day_guard_sessions == 1
        assert cfg.fill_day_sessions == 1

    def test_off_means_zero_sessions(self) -> None:
        cfg = PositionsConfig(fill_day_guard=False, fill_day_guard_sessions=3)
        assert cfg.fill_day_sessions == 0
        assert PositionsConfig(fill_day_guard_sessions=0).fill_day_sessions == 0
        with pytest.raises(ValueError):
            PositionsConfig(fill_day_guard_sessions=4)

    def test_registry_round_trip(self) -> None:
        from arc.control.registry import lookup, read_raw, write_raw

        t = lookup("positions.fill_day_guard")
        assert t.choices == ("on", "off")
        raw: dict[str, Any] = {"positions": {"fill_day_guard": True}}
        assert read_raw(t, raw) == "on"
        assert write_raw(t, "off", raw) == [(("positions", "fill_day_guard"), False)]
        assert read_raw(t, {"positions": {"fill_day_guard": False}}) == "off"
        assert read_raw(t, {}) == "on"
        n = lookup("positions.fill_day_guard_sessions")
        assert (n.min, n.max) == (0, 3)

    def test_overrides_apply(self) -> None:
        cfg = load_exit_config(overrides={("positions", "fill_day_guard"): False})
        assert cfg.positions.fill_day_sessions == 0

    def test_reason_label(self) -> None:
        assert REASON_LABELS[ReasonCode.EXIT_FILL_DAY_HOLD] == (
            "Held: opened today, thesis not broken"
        )
        assert ReasonCode.EXIT_FILL_DAY_HOLD.value == "exit:fill_day_hold"


# ---------------------------------------------------------------------------
# fixture research chain
# ---------------------------------------------------------------------------


def _fill_day_book(
    entry: str = "12.10", at: dt.datetime | None = None
) -> tuple[sqlite3.Connection, PipelineEnv, str]:
    """The E13.18 fixture book with the long call opened today (09:45 ET)."""
    from decimal import Decimal as D

    from arc.broker.base import BrokerPosition
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline import PipelineEnv
    from arc.pipeline.runner import open_db
    from tests.test_e59_research_portfolio import LONG_CALL, _open_structure

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    env = PipelineEnv.fixtures()
    sid = _open_structure(
        conn, env, LONG_CALL, stance="bullish", entry=entry, contracts=2,
        thesis="AI capex keeps SPY bid",
        at=at or FIXTURE_NOW.replace(hour=9, minute=45),
    )  # fmt: skip
    held = [BrokerPosition(symbol=LONG_CALL[0][0], qty=D(2), side="long")]
    env.positions = lambda: list(held)
    return conn, env, sid


def _watch_reply(sid: str, status: str) -> str:
    from tests.test_research_exit_watch import _research_reply

    return _research_reply(
        portfolio_view={"verdict": "concentrated", "notes": "all SPY"},
        exit_watchlist=[
            {
                "structure_id": sid,
                "ticker": "SPY",
                "action": "review",
                "thesis_status": status,
                "evidence": ["[st_9] capex guide trimmed"],
                "reason": "guide trimmed",
            }
        ],
    )


def _guard_off(settings: ArcSettings) -> ArcSettings:
    settings._yaml_overrides = {"exits": {("positions", "fill_day_guard"): False}}  # noqa: SLF001
    return settings


def _research_prompt(env: PipelineEnv) -> str:
    return env.llms["research"].prompts[0]  # type: ignore[attr-defined]


GUARD_LINE = "opened today: close only if thesis broken"


class TestChain:
    def test_fill_day_weakened_review_is_held_without_risk_call(self) -> None:
        conn, env, sid = _fill_day_book()
        _, risk = _personas(env, sid, research=_watch_reply(sid, "weakened"), risk_reply=None)
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed, report
        assert _codes(conn, "exit")["exit:fill_day_hold"] == [sid]
        row = conn.execute(
            "SELECT reason_text, payload FROM decisions WHERE reason_code = ?",
            (ReasonCode.EXIT_FILL_DAY_HOLD.value,),
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["thesis_status"] == "weakened"
        assert payload["signal_kinds"] == ["research_review"]
        assert "opened today, thesis weakened" in row["reason_text"]
        assert _kind(conn, "exit_case") == []
        from tests.test_e59_research_portfolio import _outcome

        assert _outcome(report, "quant.exit").status == "skipped"
        assert _outcome(report, "risk.exit").status == "skipped"
        assert not any("Exit review" in p for p in risk.prompts)  # type: ignore[attr-defined]
        assert _closes(conn) == []
        assert GUARD_LINE in _research_prompt(env)
        assert sid in _research_prompt(env)

    def test_fill_day_broken_review_reaches_risk(self) -> None:
        conn, env, sid = _fill_day_book()
        _, risk = _personas(
            env, sid, research=_watch_reply(sid, "broken"), risk_reply=_verdict(sid)
        )
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed, report
        assert "exit:fill_day_hold" not in _codes(conn, "exit")
        (case,) = _kind(conn, "exit_case")
        assert [t["kind"] for t in case["triggers"]] == ["research_review"]
        assert any("Exit review" in p for p in risk.prompts)  # type: ignore[attr-defined]
        (close,) = _closes(conn)
        assert close["kind"] == "close"

    def test_fill_day_take_profit_still_closes(self) -> None:
        """A profit target on the fill day is never held (Risk unavailable -> policy)."""
        conn, env, sid = _fill_day_book(entry="1.00")
        _personas(env, sid, research=_watch_reply(sid, "intact"), risk_reply="not json")
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed, report
        (case,) = _kind(conn, "exit_case")
        assert [t["kind"] for t in case["triggers"]] == ["profit_target"]
        held = conn.execute(
            "SELECT payload FROM decisions WHERE reason_code = ?",
            (ReasonCode.EXIT_FILL_DAY_HOLD.value,),
        ).fetchone()
        assert json.loads(held["payload"])["signal_kinds"] == ["research_review"]
        (close,) = _closes(conn)
        assert close["kind"] == "close"

    def test_day_after_passes_through(self) -> None:
        conn, env, sid = _fill_day_book(at=FIXTURE_NOW - dt.timedelta(days=1))
        _, risk = _personas(
            env, sid, research=_watch_reply(sid, "weakened"), risk_reply=_verdict(sid)
        )
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed, report
        assert "exit:fill_day_hold" not in _codes(conn, "exit")
        assert len(_kind(conn, "exit_case")) == 1
        assert any("Exit review" in p for p in risk.prompts)  # type: ignore[attr-defined]
        assert GUARD_LINE not in _research_prompt(env)

    def test_guard_off_is_todays_behaviour(self) -> None:
        """Golden: guard off on the fill day == guard on for a position opened earlier.

        Same personas, same replies: the research prompt (modulo the structure id),
        the exit case and the close all match the pre-E18.2 path.
        """
        runs: list[dict[str, Any]] = []
        for settings, at in (
            (_guard_off(_settings()), None),
            (_settings(), FIXTURE_NOW - dt.timedelta(days=3)),
        ):
            conn, env, sid = _fill_day_book(at=at)
            _, risk = _personas(
                env, sid, research=_watch_reply(sid, "weakened"), risk_reply=_verdict(sid)
            )
            conn, report = _run(settings, _routines(), env, conn=conn)
            assert not report.failed, report
            (case,) = _kind(conn, "exit_case")
            runs.append(
                {
                    "triggers": case["triggers"],
                    "closes": len(_closes(conn)),
                    "held": _codes(conn, "exit").get("exit:fill_day_hold"),
                    "risk_called": any("Exit review" in p for p in risk.prompts),  # type: ignore[attr-defined]
                    "guard_line": GUARD_LINE in _research_prompt(env),
                    "prompt": _research_prompt(env).replace(sid, "<sid>"),
                }
            )
        assert runs[0] == runs[1]
        assert runs[0]["held"] is None and runs[0]["closes"] == 1


class TestPromptRules:
    """``_exit_watch_rules``: the guard line is added only for guarded positions."""

    def _ctx(self, settings: ArcSettings) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(settings=settings, now=FIXTURE_NOW)

    def _pctx(self, opened_at: str) -> Any:
        from types import SimpleNamespace

        pos = SimpleNamespace(structure_id="os-1", ticker="SPY", opened_at=opened_at)
        return SimpleNamespace(positions=[pos])

    def test_lines(self) -> None:
        from arc.pipeline.steps import _exit_watch_rules

        base = _exit_watch_rules(self._ctx(_settings()))  # type: ignore[arg-type]
        assert len(base) == 2
        on = _exit_watch_rules(self._ctx(_settings()), self._pctx(OPENED_FRI))  # type: ignore[arg-type]
        assert on[:2] == base and len(on) == 3
        assert "os-1 SPY" in on[2] and GUARD_LINE in on[2]
        off = _exit_watch_rules(self._ctx(_guard_off(_settings())), self._pctx(OPENED_FRI))  # type: ignore[arg-type]
        assert off == base  # byte-identical with the guard off
        old = _exit_watch_rules(self._ctx(_settings()), self._pctx("2026-09-22T14:00:00+00:00"))  # type: ignore[arg-type]
        assert old == base
