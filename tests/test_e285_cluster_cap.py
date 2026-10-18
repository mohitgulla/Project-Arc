"""E28.5 / D131: correlation cluster cap wiring outside the pure gate.

The market helpers (stored matrix -> ``MarketSnapshot.correlations``, the open-name
list), the registry entry and reason codes, the pipeline pre-gate (prompt line + Quant
menu drop + journal) and the backtest walk. The rule itself is tested in
``tests/test_gate_cluster.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from arc.betas.corr_store import CorrRow, replace_day
from arc.config import ArcSettings
from arc.control.registry import lookup
from arc.gate import Portfolio, Position
from arc.journal.reasons import REASON_LABELS, ReasonCode, gate_reason
from arc.pipeline.market import (
    book_underlyings,
    cluster_correlations,
    cluster_matrix,
    portfolio_names,
)
from arc.store.db import connect
from arc.store.migrate import migrate

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

DAY = dt.date(2026, 10, 16)
NOW = dt.datetime(2026, 10, 16, 15, 0, tzinfo=dt.UTC)


def cfg(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"cluster_cap_enabled": True}
    return ArcSettings(_env_file=None, **(base | kw))  # type: ignore[call-arg]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _store(c: sqlite3.Connection, pairs: dict[tuple[str, str], float], day: dt.date = DAY) -> None:
    as_of = day - dt.timedelta(days=1)
    rows = [CorrRow(day, a, b, v, 60, 60, as_of) for (a, b), v in pairs.items()]
    replace_day(c, day, rows, now=NOW)


class TestRegistryAndReasons:
    def test_every_knob_is_registered(self) -> None:
        for key in ("cluster_cap_enabled", "cluster_cap_corr_threshold", "cluster_cap_max_names"):
            assert lookup(key).ref == "D131"

    def test_reason_labels(self) -> None:
        assert ReasonCode.GATE_CLUSTER_CAP in REASON_LABELS
        assert ReasonCode.CLUSTER_CAP in REASON_LABELS
        assert gate_reason("cluster_cap: SPY would be name 3") is ReasonCode.GATE_CLUSTER_CAP


class TestMarketHelpers:
    def test_matrix_is_none_when_off(self, conn: sqlite3.Connection) -> None:
        _store(conn, {("AMD", "NVDA"): 0.9})
        assert cluster_matrix(conn, cfg(cluster_cap_enabled=False), today=DAY) is None
        m = cluster_matrix(conn, cfg(), today=DAY)
        assert m is not None and m.get("NVDA", "AMD") == 0.9

    def test_correlations_known_pairs_only(self, conn: sqlite3.Connection) -> None:
        _store(conn, {("AMD", "NVDA"): 0.9, ("KO", "NVDA"): 0.1})
        m = cluster_matrix(conn, cfg(), today=DAY)
        got = cluster_correlations(m, "nvda", ["AMD", "KO", "AVGO", "NVDA"])
        assert got == {"AMD|NVDA": 0.9, "KO|NVDA": 0.1}  # AVGO unknown, self left out
        assert cluster_correlations(None, "NVDA", ["AMD"]) == {}

    def test_stale_or_empty_store_gives_no_pairs(self, conn: sqlite3.Connection) -> None:
        _store(conn, {("AMD", "NVDA"): 0.9}, day=DAY - dt.timedelta(days=30))
        m = cluster_matrix(conn, cfg(), today=DAY)
        assert cluster_correlations(m, "NVDA", ["AMD"]) == {}  # unknown -> rule counts

    def test_portfolio_names(self) -> None:
        pf = Portfolio(
            positions=[Position(underlying="QQQ", max_loss=Decimal(1))],
            reserved_underlyings=("IWM",),
        )
        assert portfolio_names(pf) == ["QQQ", "IWM"]

    def test_book_underlyings_adds_in_flight_opens(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import arc.pipeline.market as market

        monkeypatch.setattr(market, "_group_legs", lambda *_a: [("QQQ", None, [])])
        monkeypatch.setattr(
            market, "reserved_opens", lambda *_a, **_k: [("IWM", Decimal(1)), ("", Decimal(1))]
        )
        assert book_underlyings(conn, [], now=NOW) == ["QQQ", "IWM"]

    def test_build_portfolio_carries_reserved_underlyings(self, conn: sqlite3.Connection) -> None:
        from arc.pipeline.market import build_portfolio
        from tests.test_reserved_max_loss import _proposal

        _proposal(conn, "a", request="pending")
        from tests.test_gate import NOW as GNOW

        pf = build_portfolio(conn, [], None, now=GNOW, wash_sale_days=30, r=0.04)  # type: ignore[arg-type]
        assert pf.reserved_underlyings == ("SPY",)
        assert pf.reserved_max_loss == Decimal(830)
        assert "reserved_underlyings" not in pf.model_dump_json()


# ---------------------------------------------------------------------------
# Pipeline: the pre-gate (fixture propose run)
# ---------------------------------------------------------------------------


def _fixture_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
    book: list[str],
    max_names: int = 1,
    correlated: bool = False,
) -> Any:
    """The fixture propose run with *book* as the open names. With *correlated*, every
    pair of a new name with a *book* name reads corr 0.9 (a cluster peer); otherwise no
    correlations are stored and (owner, D131) unknown pairs count as not correlated."""
    import arc.pipeline.steps as steps
    from arc.gate.cluster import corr_key
    from tests.test_menu_measure import _run

    monkeypatch.setattr(steps, "book_underlyings", lambda *_a, **_k: list(book))
    if correlated:
        monkeypatch.setattr(
            steps,
            "cluster_correlations",
            lambda _m, t, names: {corr_key(t, n): 0.9 for n in names if n.upper() != t.upper()},
        )
    monkeypatch.setenv("ARC_CLUSTER_CAP_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("ARC_CLUSTER_CAP_MAX_NAMES", str(max_names))
    return _run("margin", monkeypatch, menu_measure="control")


def _cluster_rows(c: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = c.execute(
        "SELECT subject, choice, reason_text FROM decisions WHERE reason_code = ?",
        (ReasonCode.CLUSTER_CAP.value,),
    ).fetchall()
    return [dict(r) for r in rows]


def _quant_tickers(prompts: dict[str, str]) -> set[str]:
    from tests.test_menu_measure import _quant_chains

    return set(_quant_chains(prompts["quant"])) if "quant" in prompts else set()


def test_pipeline_off_and_empty_book_are_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule off, and rule on with no open names: same prompts, no cluster rows."""
    c_off, _, off = _fixture_run(monkeypatch, enabled=False, book=["ZZZ"])
    c_on, _, on = _fixture_run(monkeypatch, enabled=True, book=[])
    assert off.keys() == on.keys()
    for persona in off:
        assert off[persona] == on[persona], persona
    assert _cluster_rows(c_off) == [] and _cluster_rows(c_on) == []
    assert "Correlation cluster cap" not in "".join(off.values())


def test_pipeline_full_cluster_drops_every_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    """One correlated open name and max 1: every new name is blocked."""
    c_base, _, base = _fixture_run(monkeypatch, enabled=False, book=["ZZZ"])
    names = _quant_tickers(base)
    assert names
    conn, report, prompts = _fixture_run(monkeypatch, enabled=True, book=["ZZZ"], correlated=True)
    assert "Correlation cluster cap" in prompts["research"]
    assert "(cluster: ZZZ)" in prompts["research"]
    rows = _cluster_rows(conn)
    assert {r["subject"] for r in rows} >= names
    assert all(r["choice"] == "no_trade" for r in rows)
    assert all("correlation cluster cap" in r["reason_text"] for r in rows)
    assert _quant_tickers(prompts) == set()  # nothing priced
    assert not report.proposals


def test_pipeline_two_slots_one_peer_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    c_off, _, off = _fixture_run(monkeypatch, enabled=False, book=["ZZZ"])
    c_on, _, on = _fixture_run(monkeypatch, enabled=True, book=["ZZZ"], max_names=2)
    assert off["quant"] == on["quant"]
    assert _cluster_rows(c_on) == []


def test_pipeline_book_read_failure_leaves_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    import arc.pipeline.steps as steps
    from tests.test_menu_measure import _run

    def boom(*_a: object, **_k: object) -> list[str]:
        raise RuntimeError("broker down")

    monkeypatch.setattr(steps, "book_underlyings", boom)
    monkeypatch.setenv("ARC_CLUSTER_CAP_ENABLED", "true")
    conn, _, prompts = _run("margin", monkeypatch, menu_measure="control")
    assert "Correlation cluster cap" not in "".join(prompts.values())
    assert _cluster_rows(conn) == []


# ---------------------------------------------------------------------------
# Backtest: point-in-time correlations and the walk's rule
# ---------------------------------------------------------------------------


def test_pit_corr_matches_pit_matrix() -> None:
    """The walk's lazy per-pair values equal E28.4's point-in-time matrix."""
    from arc.backtest.correlations import pit_matrix
    from arc.backtest.sim import _PitCorr

    days = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(120)]
    a = pd.Series([100 * math.exp(0.01 * math.sin(i)) for i in range(120)], index=days)
    b = pd.Series([50 * math.exp(0.02 * math.sin(i) + 0.003 * i) for i in range(120)], index=days)

    class _T:
        def __init__(self, s: pd.Series, adj: pd.Series | None = None) -> None:
            self.closes, self.adj_closes = s, adj

    pit = _PitCorr({"AAA": _T(a * 3, adj=a), "BBB": _T(b)})  # type: ignore[dict-item]
    day = days[100]
    want = pit_matrix({"AAA": a, "BBB": b}, day).get("AAA", "BBB")
    assert want is not None
    assert pit.correlations(day, "aaa", ["BBB", "AAA", "CCC"]) == {"AAA|BBB": want}
    assert pit.correlations(day, "AAA", ["BBB"]) == {"AAA|BBB": want}  # cached
    assert pit.correlations(days[20], "AAA", ["BBB"]) == {}  # < 40 returns: unknown


def test_sim_rule_blocks_the_correlated_second_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AAA and ZZZ read corr 1.0 at every session: with max 1, the walk opens one.

    The fixture walk has < 40 returns before its decision days, so the point-in-time
    pair is pinned to 1.0 here (unknown pairs no longer count; see the next test)."""
    import arc.backtest.sim as sim
    from arc.gate.cluster import corr_key
    from tests import test_backtest_sim as tb
    from tests.test_e282_regime_gate import _Factory

    def _all_correlated(_self: object, _day: object, ticker: str, others: Any) -> Any:
        return {corr_key(ticker, o): 1.0 for o in others if o.upper() != ticker.upper()}

    monkeypatch.setattr(sim._PitCorr, "correlations", _all_correlated)
    view = tb.view.__wrapped__(_Factory(tmp_path))  # type: ignore[attr-defined]
    tickers = {t: tb._ticker(view, t) for t in ("AAA", "ZZZ")}
    off = tb._run(tickers, settings=tb._settings("margin", cluster_cap_enabled=False))
    on = tb._run(
        tickers,
        settings=tb._settings("margin", cluster_cap_enabled=True, cluster_cap_max_names=1),
    )
    roomy = tb._run(
        tickers,
        settings=tb._settings("margin", cluster_cap_enabled=True, cluster_cap_max_names=10),
    )
    assert off.skip_counts["gate_cluster_cap"] == 0
    assert on.skip_counts["gate_cluster_cap"] > 0
    assert len(on.trades) <= len(off.trades)
    assert roomy.skip_counts["gate_cluster_cap"] == 0
    assert len(roomy.trades) == len(off.trades)
    json.dumps(dict(on.skip_counts))  # reportable


def test_pipeline_unknown_correlation_blocks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner (D131 revision): no stored correlation = not correlated. Rule on, max 1, one
    open name with no data: prompts identical to rule off, no cluster rows."""
    c_off, _, off = _fixture_run(monkeypatch, enabled=False, book=["ZZZ"])
    c_on, _, on = _fixture_run(monkeypatch, enabled=True, book=["ZZZ"])
    assert off.keys() == on.keys()
    for persona in off:
        assert off[persona] == on[persona], persona
    assert _cluster_rows(c_on) == []


def test_sim_unknown_correlation_blocks_nothing(tmp_path: Path) -> None:
    """Owner (D131 revision): the fixture walk has too few returns for any pair, so every
    pair is unknown and (not correlated) the rule never skips, even at max 1."""
    from tests import test_backtest_sim as tb
    from tests.test_e282_regime_gate import _Factory

    view = tb.view.__wrapped__(_Factory(tmp_path))  # type: ignore[attr-defined]
    tickers = {t: tb._ticker(view, t) for t in ("AAA", "ZZZ")}
    off = tb._run(tickers, settings=tb._settings("margin", cluster_cap_enabled=False))
    on = tb._run(
        tickers,
        settings=tb._settings("margin", cluster_cap_enabled=True, cluster_cap_max_names=1),
    )
    assert on.skip_counts["gate_cluster_cap"] == 0
    assert len(on.trades) == len(off.trades)
