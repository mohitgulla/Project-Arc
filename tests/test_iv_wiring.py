"""E4.12 (D55) wiring: iv.record routine, iv_crosscheck [Ops] check, regime IV, `arc iv`."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest import mock

import pytest

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.iv.store import BACKFILL, FORWARD, IvRow, IvStore
from arc.monitoring import alerts, checks
from arc.routines.config import RoutinesConfig, load_routines
from arc.routines.handlers import JobContext, iv_record_source
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests.test_iv import OS_HTML, _Market

NOW = dt.datetime(2026, 10, 5, 15, 50, tzinfo=ET)
DAY = NOW.date()


@pytest.fixture
def conn() -> Any:
    c = connect(":memory:")
    migrate(c)
    return c


def _routines(writes: list[str] | None = None) -> RoutinesConfig:
    return RoutinesConfig.model_validate(
        {
            "sources": {
                "iv.record": {
                    "schedule": ["15:50"],
                    "writes": writes or [],
                    "category": "options_slow",
                    "tickers": ["AAPL"],
                }
            }
        }
    )


def _ctx(
    conn: Any, settings: ArcSettings | None = None, writes: list[str] | None = None
) -> JobContext:
    routines = _routines(writes)
    kind, spec = routines.step("iv.record")
    return JobContext(
        job="iv.record",
        kind=kind,
        spec=spec,
        run_id="run-1",
        chain_run_id=None,
        scheduled_for=NOW,
        now=NOW,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines,
        settings_factory=lambda: settings or ArcSettings(),
    )


def test_shipped_iv_record_job() -> None:
    _, spec = load_routines().jobs()["iv.record"]
    assert [t.isoformat(timespec="minutes") for t in spec.schedule] == ["15:50"]
    assert spec.writes == []


def test_iv_record_handler(conn: Any) -> None:
    def cboe(url: str) -> bytes:
        return json.dumps({"data": {"iv30": 30.0}}).encode()  # ours ~41 -> breach

    res = iv_record_source(_ctx(conn), market=_Market(379.0, 379.2), cboe_get=cboe)
    assert "3 tickers recorded" in res.summary  # AAPL + SPY + QQQ (cross-check names)
    assert "BREACH" in res.summary and res.metrics["breaches"]
    assert {r.ticker for r in IvStore(conn).rows_on(DAY, FORWARD)} == {"AAPL", "QQQ", "SPY"}


def test_iv_record_handler_all_fail(conn: Any) -> None:
    m = _Market(0.0, 0.0)
    with pytest.raises(RuntimeError, match="no IV recorded"):
        iv_record_source(_ctx(conn), market=m, cboe_get=None)


def _store_checked(conn: Any, day: dt.date, ours: float, cboe: float | None) -> None:
    detail = {"crosscheck_max_pts": 3.0}
    if cboe is not None:
        detail["cboe_iv30"] = cboe
    IvStore(conn).upsert([IvRow("SPY", day, ours, "chain_cm30", FORWARD, detail=detail)], now=NOW)


def test_iv_crosscheck_check_opens_and_resolves(conn: Any) -> None:
    r = _routines()
    assert checks.iv_crosscheck(conn, r, NOW).summary == "not judged: no recorded IV yet"
    off = RoutinesConfig.model_validate({})
    assert "disabled" in checks.iv_crosscheck(conn, off, NOW).summary

    _store_checked(conn, DAY, 0.20, 0.15)
    res = checks.iv_crosscheck(conn, r, NOW)
    assert res.severity == "degraded"
    (f,) = res.findings
    assert f.key == "iv_crosscheck" and "SPY ours 20.0 vs Cboe 15.0 (+5.0 pts)" in f.message
    n = alerts.RecordingOpsNotifier()
    out = alerts.apply(conn, [res], now=NOW, correlation={}, notifier=n)
    assert [a.key for a in out.opened] == ["iv_crosscheck"]

    later = NOW + dt.timedelta(days=1)
    _store_checked(conn, later.date(), 0.151, 0.15)
    fresh = checks.iv_crosscheck(conn, r, later)
    assert fresh.severity == "ok" and "all within threshold" in fresh.summary
    out = alerts.apply(conn, [fresh], now=later, correlation={}, notifier=n)
    assert [a.key for a in out.resolved] == ["iv_crosscheck"]
    assert "IV back within the Cboe cross-check threshold" in n.posts[-1]


def test_iv_crosscheck_ignores_unchecked_rows(conn: Any) -> None:
    _store_checked(conn, DAY, 0.20, None)
    assert "0 names" in checks.iv_crosscheck(conn, _routines(), NOW).summary


# ---------------------------------------------------------------------------
# regime entries read iv_daily (+ external fallback)
# ---------------------------------------------------------------------------


class _Bars(_Market):
    def history_bars(self, symbol, start, end, timeframe="1Day"):  # noqa: ANN001, ANN201
        from arc.data.base import HistoryBar

        out = []
        d = start
        i = 0
        while d <= end:
            if d.weekday() < 5:
                ts = dt.datetime.combine(d, dt.time(4), tzinfo=dt.UTC)
                c = 100.0 + (i % 7)
                out.append(HistoryBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=1.0))
                i += 1
            d += dt.timedelta(days=1)
        return out


def _regime(conn: Any, settings: ArcSettings) -> dict[str, Any]:
    from arc.pipeline.env import PipelineEnv
    from arc.pipeline.steps import _regime_entries

    ctx = _ctx(conn, settings, writes=["regime"])
    env = PipelineEnv(
        market=_Bars(379.0, 379.2),
        account=lambda: None,  # type: ignore[arg-type,return-value]
        positions=list,
        llms={},
    )
    assert _regime_entries(ctx, env, ["AAPL"]) == ["AAPL"]
    entry = ContextStore(conn).snapshot(NOW).latest("regime", "AAPL")
    assert entry is not None
    return entry.payload["vol"]


def test_regime_short_history_shows_labelled_external_percentile(conn: Any) -> None:
    from arc.iv import optionstrategist as osf

    rows = [r.to_iv_row() for r in osf.parse(OS_HTML.replace("AAA ", "AAPL"))]
    IvStore(conn).upsert(rows, now=NOW)
    vol = _regime(conn, ArcSettings())
    assert vol["iv"] == pytest.approx(0.42, abs=0.01)  # live 30-DTE IV from the chain
    assert vol["iv_rank"] is None and vol["iv_percentile"] is None
    assert vol["iv_percentile_ext"] == 0.15
    assert vol["iv_percentile_ext_source"] == "optionstrategist@2026-10-02"


def test_regime_external_too_old_is_dropped(conn: Any) -> None:
    from arc.iv import optionstrategist as osf

    IvStore(conn).upsert(
        [r.to_iv_row() for r in osf.parse(OS_HTML.replace("AAA ", "AAPL"))], now=NOW
    )
    vol = _regime(conn, ArcSettings(iv_ext_max_age_days=1))
    assert vol["iv_percentile_ext"] is None


def test_regime_rank_from_own_series(conn: Any) -> None:
    days = [DAY - dt.timedelta(days=i) for i in range(1, 160)][::-1]
    IvStore(conn).upsert(
        [IvRow("AAPL", d, 0.30 + 0.001 * i, "bars_bs_cm30", BACKFILL) for i, d in enumerate(days)]
        + [IvRow("AAPL", DAY, 0.50, "chain_cm30", FORWARD)],
        now=NOW,
    )
    vol = _regime(conn, ArcSettings())
    assert vol["iv"] == 0.50 and vol["iv_rank"] == 1.0 and vol["iv_percentile"] == 1.0
    assert vol["iv_percentile_ext"] is None
    assert vol["iv_observations"] == 160


# ---------------------------------------------------------------------------
# `arc iv` CLI (offline parts)
# ---------------------------------------------------------------------------


def test_cli_import_validate_status_csv(tmp_path: Any, capsys: Any) -> None:
    from arc.cli import main

    db = str(tmp_path / "arc.db")
    page = tmp_path / "os.html"
    page.write_text(OS_HTML)
    with mock.patch("arc.utils.calendar.now_et", return_value=NOW):
        assert main(["iv", "status", "--db", db]) == 0
        assert "iv_daily is empty" in capsys.readouterr().out
        assert main(["iv", "validate", "--db", db, "--no-hv", "--tickers", "all"]) == 1
        capsys.readouterr()
        rc = main(["iv", "import-optionstrategist", "--db", db, "--file", str(page),
                   "--tickers", "AAA,ZZZ"])  # fmt: skip
        out = capsys.readouterr().out
        assert rc == 0 and "stored 3 optionstrategist rows" in out
        assert "AAA     2026-10-02  22.50   600   15  23.0" in out and "1/2 watch names" in out

        csvdir = tmp_path / "ivh"
        csvdir.mkdir()
        (csvdir / "AAA.csv").write_text("date,atm_iv\n2026-10-01,0.2\n2026-10-02,0.21\n")
        assert main(["iv", "import-csv", "--db", db, "--dir", str(csvdir)]) == 0
        assert "imported 2 rows from 1 file" in capsys.readouterr().out
        assert main(["iv", "import-csv", "--db", db, "--dir", str(tmp_path / "none")]) == 0
        assert "nothing to import" in capsys.readouterr().out

        assert main(["iv", "validate", "--db", db, "--no-hv", "--tickers", "all"]) == 0
        out = capsys.readouterr().out
        assert "AAA" in out and "overall:" in out
        assert main(["iv", "status", "--db", db]) == 0
        out = capsys.readouterr().out
        assert (
            "alpaca_cm30       AAA         2" in out
        )  # legacy CSV = forward series and "optionstrategist" in out
    (bad := tmp_path / "bad.html").write_text("<html>nothing</html>")
    assert main(["iv", "import-optionstrategist", "--db", db, "--file", str(bad)]) == 1
