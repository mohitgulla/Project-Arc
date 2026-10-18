"""E28.2 / D131: short-premium regime gate wiring outside the pure gate.

The SPY trend reader (``arc.pipeline.market.market_trend``), the pre-gate menu filter and
its journal row, the backtest walk's point-in-time SMA, the registry entries and the
regime schema bump. The gate rule itself is tested in ``tests/test_gate_regime.py``.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.context.kinds import KINDS, RegimePayload
from arc.control.registry import REGISTRY, lookup
from arc.features.technicals import compute_technicals
from arc.gate import MarketTrend
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.pipeline.market import market_snapshot, market_trend
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

NOW = dt.datetime(2026, 10, 16, 11, 0, tzinfo=ET)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "arc.db")
    migrate(c)
    yield c
    c.close()


def on(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, regime_gate_enabled=True, **kw)  # type: ignore[call-arg]


def write_regime(
    conn: sqlite3.Connection,
    *,
    close: float | None = 590.0,
    sma200: float | None = 600.0,
    sma50: float | None = 580.0,
    sma100: float | None = 595.0,
    bar: str = "2026-10-15",
    at: dt.datetime = NOW - dt.timedelta(hours=20),
    subject: str = "SPY",
) -> None:
    from tests.test_features_technicals import _payload

    p = json.loads(json.dumps(_payload()))
    p["ticker"] = subject
    p["technicals"].update(close=close, sma50=sma50, sma100=sma100, sma200=sma200, bar_date=bar)
    ContextStore(conn).write(
        kind="regime",
        subject=subject,
        payload=RegimePayload.model_validate(p),
        produced_by="research",
        ttl="1 session",
        now=at,
    )
    conn.commit()


class TestMarketTrend:
    def test_reads_the_latest_spy_regime_even_once_expired(self, conn: sqlite3.Connection) -> None:
        write_regime(conn, close=610.0, bar="2026-10-14", at=NOW - dt.timedelta(days=2))
        write_regime(conn)  # yesterday's, past its 1-session TTL at NOW
        t = market_trend(conn, on(), now=NOW)
        assert t == MarketTrend(
            ticker="SPY",
            close=Decimal("590.0"),
            ma=200,
            sma=Decimal("600.0"),
            as_of=dt.date(2026, 10, 15),
        )

    def test_ignores_rows_valid_after_now_and_other_subjects(
        self, conn: sqlite3.Connection
    ) -> None:
        write_regime(conn, subject="QQQ")
        write_regime(conn, at=NOW + dt.timedelta(hours=1))
        assert market_trend(conn, on(), now=NOW) is None

    @pytest.mark.parametrize(("ma", "sma"), [(50, "580.0"), (100, "595.0")])
    def test_other_ma(self, conn: sqlite3.Connection, ma: int, sma: str) -> None:
        write_regime(conn)
        t = market_trend(conn, on(regime_gate_ma=ma), now=NOW)
        assert t is not None and t.ma == ma and t.sma == Decimal(sma)

    @pytest.mark.parametrize("kw", [{"sma200": None}, {"sma200": 0.0}, {"close": 0.0}])
    def test_incomplete_row_is_none(self, conn: sqlite3.Connection, kw: dict[str, Any]) -> None:
        write_regime(conn, **kw)
        assert market_trend(conn, on(), now=NOW) is None

    def test_row_without_technicals_is_none(self, conn: sqlite3.Connection) -> None:
        from tests.test_features_technicals import _payload

        p = _payload() | {"ticker": "SPY", "technicals": None}
        ContextStore(conn).write(
            kind="regime",
            subject="SPY",
            payload=RegimePayload.model_validate(p),
            produced_by="research",
            ttl="1 session",
            now=NOW - dt.timedelta(hours=1),
        )
        assert market_trend(conn, on(), now=NOW) is None

    def test_off_reads_nothing(self, conn: sqlite3.Connection) -> None:
        write_regime(conn)
        off = ArcSettings(_env_file=None, regime_gate_enabled=False)  # type: ignore[call-arg]
        assert market_trend(conn, off, now=NOW) is None

    def test_snapshot_carries_the_trend(self) -> None:
        t = MarketTrend(ticker="SPY", close=Decimal(1), ma=200, sma=Decimal(2), as_of=NOW.date())
        assert market_snapshot({}, {}, trend=t).market_trend == t
        assert market_snapshot({}, {}).market_trend is None


class TestTechnicalsSma100:
    def test_sma100_is_the_mean_of_the_last_100_closes(self) -> None:
        from tests.test_features_technicals import AS_OF, _nvda

        df = _nvda()
        t = compute_technicals(df, AS_OF)
        assert t is not None and t.sma100 is not None
        upto = df[[d <= AS_OF for d in df.index]]
        assert t.sma100 == pytest.approx(float(upto["close"].iloc[-100:].mean()))

    def test_short_history_leaves_sma100_none(self) -> None:
        from tests.test_features_technicals import AS_OF, _nvda

        df = _nvda()
        short = df[[d <= AS_OF for d in df.index]].iloc[-60:]
        t = compute_technicals(short, AS_OF)
        assert t is not None and t.sma100 is None

    def test_regime_schema_v7_still_reads_v6_rows(self) -> None:
        from tests.test_features_technicals import _payload

        assert KINDS["regime"].schema_version == 7
        p = _payload()
        p["technicals"].pop("sma100", None)
        assert RegimePayload.model_validate(p).technicals.sma100 is None  # type: ignore[union-attr]


class TestRegistry:
    def test_every_knob_is_registered(self) -> None:
        keys = set(REGISTRY)
        assert {
            "regime_gate_enabled",
            "regime_gate_ma",
            "regime_gate_buffer_pct",
            "regime_gate_blocked_kinds",
        } <= keys
        kinds = lookup("regime_gate_blocked_kinds")
        assert kinds.choices == ("bull_put,iron_condor", "bull_put", "iron_condor")

    def test_reason_labels(self) -> None:
        assert ReasonCode.GATE_SHORT_PREMIUM_REGIME in REASON_LABELS
        assert ReasonCode.REGIME_GATE_CREDIT_OFF in REASON_LABELS
        from arc.gate import RuleCode
        from arc.journal.reasons import gate_reason

        v = f"{RuleCode.SHORT_PREMIUM_REGIME}: SPY close 1 < SMA200 2"
        assert gate_reason(v) is ReasonCode.GATE_SHORT_PREMIUM_REGIME


# ---------------------------------------------------------------------------
# Pipeline: the pre-gate menu filter + journal (fixture propose run)
# ---------------------------------------------------------------------------


def _fixture_run(monkeypatch: pytest.MonkeyPatch, *, enabled: bool, regime: str | None) -> Any:
    """The fixture propose run on the margin profile (bull put + condor in the menus).

    *regime*: ``"below"`` / ``"above"`` seeds a SPY regime row the trend reader picks
    up; ``None`` leaves the fixture's SPY row (no SMA200: fail closed).
    """
    import arc.pipeline.steps as steps
    from tests.test_menu_measure import _run

    if regime is not None:
        close = 500.0 if regime == "below" else 900.0
        trend = MarketTrend(
            ticker="SPY",
            close=Decimal(str(close)),
            ma=200,
            sma=Decimal("700"),
            as_of=dt.date(2026, 9, 25),
        )
        monkeypatch.setattr(steps, "market_trend", lambda *_a, **_k: trend)
    monkeypatch.setenv("ARC_REGIME_GATE_ENABLED", "true" if enabled else "false")
    return _run("margin", monkeypatch, menu_measure="control")


def _menu_kinds(prompts: dict[str, str]) -> set[str]:
    from tests.test_menu_measure import _quant_chains

    if "quant" not in prompts:
        return set()
    return {r["strategy"] for c in _quant_chains(prompts["quant"]).values() for r in c["menu"]}


def _regime_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT subject, choice, reason_text, payload FROM decisions WHERE reason_code = ?",
        (ReasonCode.REGIME_GATE_CREDIT_OFF.value,),
    ).fetchall()
    return [dict(r) for r in rows]


def test_pipeline_below_sma_drops_bull_puts_and_condors(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _, prompts = _fixture_run(monkeypatch, enabled=True, regime="below")
    kinds = _menu_kinds(prompts)
    assert "bull_put" not in kinds and "iron_condor" not in kinds
    assert kinds  # the debit kinds still reach Quant
    rows = _regime_rows(conn)
    (session,) = [r for r in rows if r["subject"] == "session"]
    payload = json.loads(session["payload"])
    assert payload["market_trend"]["close"] == "500.0"
    assert payload["blocked_kinds"] == ["bull_put", "iron_condor"]
    assert "SPY close 500.0 < SMA200 700.00" in session["reason_text"]
    # SPY (neutral: iron condor only) has nothing left -> one no-trade row
    assert any(r["subject"] == "SPY" and r["choice"] == "no_trade" for r in rows)


def test_pipeline_missing_trend_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _, prompts = _fixture_run(monkeypatch, enabled=True, regime=None)
    assert not {"bull_put", "iron_condor"} & _menu_kinds(prompts)
    (session,) = [r for r in _regime_rows(conn) if r["subject"] == "session"]
    assert "regime input missing" in session["reason_text"]


def test_pipeline_above_sma_and_off_are_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate open (SPY above) and gate off give the same Quant prompt and no journal row."""
    c_off, _, off = _fixture_run(monkeypatch, enabled=False, regime="below")
    c_up, _, up = _fixture_run(monkeypatch, enabled=True, regime="above")
    assert {"bull_put", "iron_condor"} <= _menu_kinds(off)
    assert off["quant"] == up["quant"]
    assert _regime_rows(c_off) == [] and _regime_rows(c_up) == []


# ---------------------------------------------------------------------------
# Backtest: the point-in-time SMA and the walk's rule
# ---------------------------------------------------------------------------


def test_market_trends_point_in_time() -> None:
    from arc.backtest.sim import market_trends

    days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(6)]
    closes = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0, 60.0], index=days)
    got = market_trends(closes, 3, [*days, dt.date(2026, 2, 1)])
    assert set(got) == set(days[2:])  # < 3 closes and a session without a close: no entry
    assert got[days[2]].sma == Decimal("20.0") and got[days[2]].close == Decimal("30.0")
    assert got[days[5]].sma == Decimal("50.0")  # 40, 50, 60: never a later close


def test_sim_rule_blocks_credit_below_and_reports_it(tmp_path: Path) -> None:
    """The sim walk refuses the blocked kinds on a below-SMA day and counts the skip."""
    from tests import test_backtest_sim as tb

    view = tb.view.__wrapped__(_Factory(tmp_path))  # type: ignore[attr-defined]
    tickers = {"AAA": tb._ticker(view, "AAA")}
    settings = tb._settings("margin", regime_gate_enabled=True)
    base = tb._run(tickers, settings=settings)  # trends=None: rule not applied
    below = {
        d: MarketTrend(ticker="SPY", close=Decimal(1), ma=200, sma=Decimal(2), as_of=d)
        for d in tb.DAYS
    }
    above = {d: t.model_copy(update={"close": Decimal(3)}) for d, t in below.items()}
    blocked = tb._run(tickers, settings=settings, trends=below)
    missing = tb._run(tickers, settings=settings, trends={})
    clear = tb._run(tickers, settings=settings, trends=above)
    credit = base.trades["kind"].isin(["vertical_credit", "iron_condor"])
    assert credit.any()
    for run in (blocked, missing):
        assert run.skip_counts["gate_short_premium_regime"] > 0
        legs = run.trades["legs"] if len(run.trades) else pd.Series([], dtype=str)
        # no bull put (a credit vertical of puts) and no condor opened
        assert not any(" " in lg and lg.count("P") >= 2 and "C" not in lg for lg in legs)
        assert not (run.trades["kind"] == "iron_condor").any() if len(run.trades) else True
    assert clear.skip_counts["gate_short_premium_regime"] == 0
    assert len(clear.trades) == len(base.trades)
    off = tb._run(tickers, settings=tb._settings("margin", regime_gate_enabled=False), trends=below)
    assert off.skip_counts["gate_short_premium_regime"] == 0


class _Factory:
    """A ``tmp_path_factory`` stand-in for calling the module-scoped ``view`` fixture."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def mktemp(self, name: str) -> Path:
        p = self.root / name
        p.mkdir()
        return p
