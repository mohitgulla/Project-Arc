"""E16.1 (D76): nightly ``iv.backfill`` top-up (selection, runtime stop, resume, summary)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.control.effective import effective_routines
from arc.control.registry import NOT_EXPOSED_PATHS, REGISTRY
from arc.control.service import ControlService
from arc.iv import topup
from arc.iv.store import BACKFILL, FORWARD, IvRow, IvStore
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.handlers import JobContext, iv_backfill_source
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET, sessions_between
from tests.test_iv import _Hist

NOW = dt.datetime(2026, 10, 9, 16, 20, tzinfo=ET)


class _AnyHist(_Hist):
    """``_Hist`` for multi-letter tickers: option symbols are ``Q<yymmdd><c|p><K>``."""

    def contracts(self, ticker, exp_start, exp_end, lo, hi):  # noqa: ANN001, ANN201
        return super().contracts("Q", exp_start, exp_end, lo, hi)


LOOKBACK = [dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(10)]


@pytest.fixture
def conn() -> Any:
    c = connect(":memory:")
    migrate(c)
    return c


def _fill(conn: Any, ticker: str, days: list[dt.date], source: Any = BACKFILL) -> None:
    IvStore(conn).upsert([IvRow(ticker, d, 0.3, "x", source) for d in days], now=NOW)


def _skip(conn: Any, ticker: str, days: list[dt.date]) -> None:
    for d in days:
        IvStore(conn).skip(ticker, d, BACKFILL, "no traded call bar", now=NOW)


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def test_candidates_order_open_first_then_active_then_reference() -> None:
    assert topup.candidates(["TSLA", "META"], ["aapl", "META", "SPY", " "]) == [
        "META", "TSLA", "AAPL", "SPY", "QQQ", "IWM",
    ]  # fmt: skip


def test_coverage_counts_both_sources_and_skips(conn: Any) -> None:
    _fill(conn, "A", LOOKBACK[:3])
    _fill(conn, "A", LOOKBACK[2:5], FORWARD)  # day 2 in both: counted once
    _fill(conn, "A", [dt.date(2026, 5, 1)])  # before the lookback: not counted
    _skip(conn, "A", LOOKBACK[5:7])
    c = topup.coverage(IvStore(conn), "a", LOOKBACK)
    assert (c.ticker, c.usable, c.missing, c.unskipped) == ("A", 5, 5, 3)
    assert c.short(6) and not c.short(5)


def test_select_short_vs_full_skips_cap_and_order(conn: Any) -> None:
    store = IvStore(conn)
    _fill(conn, "FULL", LOOKBACK)  # 10/10: not short
    _fill(conn, "MOST", LOOKBACK[:8])  # 8: not short at min_obs 8
    _fill(conn, "DONE", LOOKBACK[:2])
    _skip(conn, "DONE", LOOKBACK[2:])  # short but every gap skipped: exhausted
    # NEW1..NEW3 have nothing at all
    names = ["FULL", "NEW2", "DONE", "MOST", "NEW1", "NEW3"]
    picked, short, exhausted = topup.select(store, names, LOOKBACK, min_obs=8, max_tickers=2)
    assert picked == ["NEW2", "NEW1"]  # input order, capped
    assert [c.ticker for c in short] == ["NEW2", "NEW1", "NEW3"]
    assert [c.ticker for c in exhausted] == ["DONE"]
    # A partly skipped name with one untried day is still picked.
    store.conn.execute(
        "DELETE FROM iv_skips WHERE ticker='DONE' AND day=?", (LOOKBACK[9].isoformat(),)
    )
    store.conn.commit()
    picked, _, exhausted = topup.select(store, ["DONE"], LOOKBACK, min_obs=8, max_tickers=5)
    assert picked == ["DONE"] and exhausted == []


# ---------------------------------------------------------------------------
# run: backfill, runtime stop, resumability, summary
# ---------------------------------------------------------------------------


def _sessions() -> list[dt.date]:
    return [dt.date(2024, 3, 4) + dt.timedelta(days=i) for i in range(5)]


def test_run_topup_fills_then_is_a_noop(conn: Any) -> None:
    sessions = _sessions()
    made: list[_Hist] = []

    def factory() -> _Hist:
        made.append(_Hist(sessions))
        return made[-1]

    kw: dict[str, Any] = dict(
        sessions=sessions, lookback=sessions, now=NOW, min_obs=5, max_tickers=10,
        max_runtime_s=900, r=0.04, dividend_yields={},
    )  # fmt: skip
    res = topup.run_topup(conn, factory, ["X", "Y"], **kw)
    assert res.picked == ["X", "Y"]
    assert res.filled == len(res.picked) and res.days_added == 5 * len(res.picked)
    assert res.still_short == [] and res.report is not None and res.report.deferred == []
    assert res.summary().startswith(
        f"{res.filled} tickers filled, {res.days_added} days added, 0 skipped, 0 still short"
    )
    m = res.metrics()
    assert m["per_ticker"]["X"] == {"stored": 5, "skipped": 0} and m["deferred"] == []
    # Second night: everything full, no Alpaca client is even built.
    again = topup.run_topup(conn, factory, ["X", "Y"], **kw)
    assert again.picked == [] and again.report is None and len(made) == 1
    assert again.summary() == "no short names: IV rank history complete"
    assert again.metrics()["per_ticker"] == {} and again.days_added == 0


def test_run_topup_runtime_stop_defers_and_next_run_resumes(conn: Any) -> None:
    sessions = _sessions()
    ticks = iter([0.0, 0.0, 10.0, 999.0, 999.0])  # t0, A, B, C over budget, wall_s

    kw: dict[str, Any] = dict(
        sessions=sessions, lookback=sessions, now=NOW, min_obs=5, max_tickers=3,
        r=0.04, dividend_yields={},
    )  # fmt: skip
    res = topup.run_topup(
        conn, lambda: _Hist(sessions), ["A", "B", "C"], max_runtime_s=100,
        clock=lambda: next(ticks), **kw,
    )  # fmt: skip
    assert res.report is not None
    assert [t.ticker for t in res.report.tickers] == ["A", "B"]
    assert res.report.deferred == ["C"]
    assert res.still_short == ["C"]
    s = res.summary()
    assert "2 tickers filled, 10 days added" in s and "1 still short (C)" in s
    assert "1 deferred by max_runtime_s" in s
    # Next run: A and B are full, C is picked and finished.
    res2 = topup.run_topup(conn, lambda: _Hist(sessions), ["A", "B", "C"], max_runtime_s=100, **kw)
    assert res2.picked == ["C"] and res2.days_added == 5 and res2.still_short == []


def test_run_topup_skips_reported_and_exhausted_not_retried(conn: Any) -> None:
    sessions = [dt.date(2024, 3, 4), dt.date(2024, 3, 5)]
    h = _Hist(sessions)
    h.exps = [sessions[0] + dt.timedelta(days=21)]  # no expiry above 30 DTE: every day skips
    kw: dict[str, Any] = dict(
        sessions=sessions, lookback=sessions, now=NOW, min_obs=2, max_tickers=5,
        max_runtime_s=900, r=0.04, dividend_yields={},
    )  # fmt: skip
    res = topup.run_topup(conn, lambda: h, ["Z"], **kw)
    assert res.days_added == 0 and sum(res.skip_reasons.values()) == 2
    assert "skipped (incomplete 30-DTE bracket" in res.summary()
    # Every gap now has a skip row: Z is exhausted, not retried.
    res2 = topup.run_topup(conn, lambda: h, ["Z"], **kw)
    assert res2.picked == [] and [c.ticker for c in res2.exhausted] == ["Z"]
    assert "exhausted (all gaps skipped)" in res2.summary()


# ---------------------------------------------------------------------------
# routine wiring
# ---------------------------------------------------------------------------


def _ctx(conn: Any, options: dict[str, Any] | None = None) -> JobContext:
    routines = RoutinesConfig.model_validate(
        {
            "sources": {
                "iv.backfill": {"schedule": ["16:20"], "writes": [], **(options or {})},
            },
            "iv_backfill": {"max_tickers_per_run": 2, "max_runtime_s": 900,
                            "since": "2026-09-28"},
        }
    )  # fmt: skip
    kind, spec = routines.step("iv.backfill")
    return JobContext(
        job="iv.backfill", kind=kind, spec=spec, run_id="r1", chain_run_id=None,
        scheduled_for=NOW, now=NOW, conn=conn, snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines, settings_factory=lambda: ArcSettings(),
    )  # fmt: skip


def test_handler_fills_capped_names_with_explicit_tickers(conn: Any) -> None:
    sessions = sessions_between(dt.date(2026, 9, 28), dt.date(2026, 10, 8))
    hists: list[_Hist] = []

    def factory() -> _AnyHist:
        hists.append(_AnyHist(sessions))
        return hists[-1]

    res = iv_backfill_source(_ctx(conn, {"tickers": ["aapl", "msft", "nvda"]}), factory)
    assert res.metrics["picked"] == ["AAPL", "MSFT"]  # cap 2, active order
    assert res.metrics["days_added"] == 2 * len(sessions)
    assert IvStore(conn).days("AAPL", BACKFILL) == set(sessions)  # until = previous session
    assert "2 tickers filled" in res.summary


def test_handler_uses_open_underlyings_first(conn: Any) -> None:
    from arc.universe.tiers import core_tickers
    from tests.test_day_trades import _structure

    sessions = sessions_between(dt.date(2026, 9, 28), dt.date(2026, 10, 8))
    res = iv_backfill_source(_ctx(conn), lambda: _AnyHist(sessions))
    # No stored active list: the core list from settings, capped at 2.
    assert res.metrics["picked"] == core_tickers(ArcSettings())[:2]
    conn.execute("PRAGMA foreign_keys = OFF")  # a bare open_structures row
    _structure(conn, "s1", NOW - dt.timedelta(days=3), None)
    conn.execute("UPDATE open_structures SET ticker = 'ZZZT', status = 'open' WHERE id = 's1'")
    res = iv_backfill_source(_ctx(conn), lambda: _AnyHist(sessions))
    assert res.metrics["picked"][0] == "ZZZT"  # open underlying before the active list


def test_handler_raises_when_every_ticker_errors(conn: Any) -> None:
    class Bad(_Hist):
        def underlying_closes(self, *a: Any) -> dict[dt.date, float]:
            raise RuntimeError("alpaca 500")

    with pytest.raises(RuntimeError, match="failed for every picked ticker"):
        iv_backfill_source(_ctx(conn, {"tickers": ["AAPL"]}), lambda: Bad([]))


def test_handler_skips_when_since_is_after_yesterday(conn: Any) -> None:
    from arc.routines.handlers import JobSkippedError

    ctx = _ctx(conn)
    ctx.routines = RoutinesConfig.model_validate(
        {"sources": {"iv.backfill": {"schedule": ["16:20"], "writes": []}},
         "iv_backfill": {"since": "2026-10-09"}}
    )  # fmt: skip
    with pytest.raises(JobSkippedError, match="no sessions"):
        iv_backfill_source(ctx, lambda: _Hist([]))


def test_shipped_job_slot_after_iv_record_and_knobs() -> None:
    c = load_routines()
    _, spec = c.jobs()["iv.backfill"]
    _, rec = c.jobs()["iv.record"]
    assert [t.strftime("%H:%M") for t in spec.schedule] == ["16:20"]
    assert spec.schedule[0] > rec.schedule[0]
    assert spec.writes == [] and spec.lane.value == "background"
    assert spec.ttl is not None
    assert c.iv_backfill.max_tickers_per_run == 10
    assert c.iv_backfill.max_runtime_s == 900
    assert c.iv_backfill.since == dt.date(2024, 3, 1)


def test_registry_entries() -> None:
    assert REGISTRY["iv_backfill.max_tickers_per_run"].path == (
        "iv_backfill",
        "max_tickers_per_run",
    )
    assert REGISTRY["iv_backfill.max_runtime_s"].hard_ceiling == 3600
    assert "iv_backfill.since" in NOT_EXPOSED_PATHS and "iv_backfill.since" not in REGISTRY


def test_slack_override_reaches_the_effective_routines(conn: Any) -> None:
    base = ArcSettings(_env_file=None, approver_slack_user_ids=["U0OWNER"])  # type: ignore[call-arg]
    svc = ControlService(conn, base=base, now=lambda: NOW)
    r = svc.set("iv_backfill.max_tickers_per_run", "25", actor="U0OWNER", source="slack")
    assert r.outcome == "applied"  # Risk.NONE: applies at once
    assert effective_routines(conn).iv_backfill.max_tickers_per_run == 25


def test_regime_line_prints_iv_rank_as_0_to_100() -> None:
    """E16.1: iv_rank is 0..1; the Research line shows it 0..100 (was `ivr 0` / `ivr 1`)."""
    from arc.personas.builders import regime_line

    vol = {"iv": 0.3, "hv20": 0.25, "iv_hv20_ratio": 1.2}
    assert "ivr 37 " in regime_line("AAPL", {"vol": {**vol, "iv_rank": 0.374}, "last_close": 1})
    assert "ivr 100" in regime_line("AAPL", {"vol": {**vol, "iv_rank": 1.0}})
    assert regime_line("AAPL", {"vol": {**vol, "iv_rank": None}}).endswith("ivr n/a")
