"""E22.5 (D97): per-sleeve reporting across the journal, the Tower, the Day Recap.

Fixture: the Tower history store (``scripts/tower_fixture_db.py --history``) with entry
DTEs rewritten so closed trades sit in both sleeves of the cash_debit profile
(``short`` 10–29, ``swing`` 30–60), plus a pre-D97 30–60 trade (stored before the D80
epoch, no stored sleeve: maps to ``swing`` by its DTE) and trades whose DTE fits neither
(``other``).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from arc.account_profiles import DteSleeve
from arc.config import ArcSettings
from arc.journal.scorecard import closed_positions
from arc.journal.sleeves import (
    MIN_CLOSED,
    OTHER,
    bootstrap_mean_ci,
    day_sleeve_lines,
    sleeve_name,
    sleeve_report,
    sleeve_report_lines,
    sleeves_by_structure,
    verdict,
)
from arc.tower.api import create_app
from arc.tower.data import connect_ro
from arc.tower.data_performance import load_breakdown, load_performance
from arc.tower.data_sleeves import sleeve_meta, sleeve_usage
from arc.tower.data_trades import TradeFilters, load_filter_options, load_trades
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 9, 28, 15, 40, tzinfo=ET)
SLEEVES = [
    DteSleeve(name="short", dte_min=10, dte_max=29, max_positions=5, exit_policy="short"),
    DteSleeve(name="swing", dte_min=30, dte_max=60, max_positions=5),
]
EPOCH = dt.datetime(2026, 8, 1, tzinfo=ET)  # the fixture history runs ~Jun..Sep


def _load(name: str, path: Path):  # noqa: ANN202 - module loaded by path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


fixture = _load("tower_fixture_db", REPO / "scripts" / "tower_fixture_db.py")


def _set_dte(conn: sqlite3.Connection, sid: str, dte: int) -> None:
    for table, key in (("open_structures", "id"),):
        conn.execute(
            f"UPDATE {table} SET structure_json = json_set(structure_json, '$.dte', ?) "  # noqa: S608
            f"WHERE {key} = ?",
            (dte, sid),
        )
    conn.execute(
        """UPDATE proposals SET structure_json = json_set(structure_json, '$.dte', ?)
           WHERE proposal_hash = (SELECT open_proposal_hash FROM open_structures WHERE id = ?)""",
        (dte, sid),
    )


@pytest.fixture(scope="module")
def layout(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Build once; rewrite DTEs: closed trades in order of close -> short/swing/other."""
    path = tmp_path_factory.mktemp("sleeves") / "arc.db"
    fixture.build(path, NOW, history=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    closed = closed_positions(
        conn, start=dt.datetime(2000, 1, 1, tzinfo=ET), end=NOW, now=NOW, all_time=True
    )
    closed.sort(key=lambda c: (c.opened_at or c.closed_at, c.structure_id))
    pre = [c for c in closed if c.opened_at is not None and c.opened_at < EPOCH]
    post = [c for c in closed if c not in pre]
    assert pre and len(post) >= 6, (len(pre), len(post))
    by_id: dict[str, int] = {}
    # pre-D97 (before the epoch): a 30-60 trade -> swing by its entry DTE
    by_id[pre[0].structure_id] = 45
    for c in pre[1:]:
        by_id[c.structure_id] = 20
    # after the epoch: alternate short / swing, one 5 DTE and one 75 DTE -> other
    for i, c in enumerate(post):
        by_id[c.structure_id] = (14, 40)[i % 2]
    by_id[post[-1].structure_id] = 5
    by_id[post[-2].structure_id] = 75
    for sid, dte in by_id.items():
        _set_dte(conn, sid, dte)
    # open book: 2 short, 1 swing
    opens = [r[0] for r in conn.execute("SELECT id FROM open_structures WHERE status = 'open'")]
    for sid, dte in zip(opens, (12, 25, 33), strict=False):
        _set_dte(conn, sid, dte)
    conn.execute(
        "INSERT OR REPLACE INTO routine_state(key, value, updated_at) VALUES (?, ?, ?)",
        ("store:epoch", json.dumps({"started_at": EPOCH.isoformat()}), NOW.isoformat()),
    )
    conn.commit()
    conn.close()
    expect = {sid: sleeve_name(SLEEVES, dte) for sid, dte in by_id.items()}
    return SimpleNamespace(path=path, dte=by_id, sleeve=expect, pre=pre[0].structure_id)


@pytest.fixture
def conn(layout: SimpleNamespace):
    c = connect_ro(layout.path)
    yield c
    c.close()


def _settings() -> ArcSettings:
    return ArcSettings(account_profile="cash_debit")


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def test_sleeve_is_derived_from_entry_dte(layout: SimpleNamespace, conn) -> None:  # noqa: ANN001
    got = sleeves_by_structure(conn, SLEEVES)
    for sid, name in layout.sleeve.items():
        assert got[sid] == name
    assert got[layout.pre] == "swing"  # pre-D97 45 DTE, no stored sleeve
    assert {got[s] for s in layout.sleeve} == {"short", "swing", OTHER}
    assert sleeve_name(SLEEVES, 29) == "short" and sleeve_name(SLEEVES, 30) == "swing"
    assert sleeve_name(SLEEVES, None) == OTHER and sleeve_name(SLEEVES, 61) == OTHER


def test_sleeve_report_counts_every_trade_once(layout: SimpleNamespace, conn) -> None:  # noqa: ANN001
    rep = sleeve_report(conn, SLEEVES, since=None, until=NOW, now=NOW)
    names = [s.sleeve for s in rep.sleeves]
    assert names == ["short", "swing", OTHER]
    closed = closed_positions(conn, start=dt.datetime(1970, 1, 1, tzinfo=dt.UTC), end=NOW, now=NOW)
    assert sum(s.closed for s in rep.sleeves) == len(closed)
    for s in rep.sleeves:
        mine = [c for c in closed if layout.sleeve.get(c.structure_id) == s.sleeve]
        assert s.closed == len(mine)
        assert s.realised == pytest.approx(sum(c.realised_pnl for c in mine))
        if mine:
            assert s.mean_pnl == pytest.approx(s.realised / len(mine))
            assert s.win_rate == pytest.approx(sum(c.realised_pnl > 0 for c in mine) / len(mine))
            assert sum(s.exit_reasons.values()) == len(mine)
            assert s.mean_days_held is not None and s.mean_days_held >= 0
            assert s.mean_net_pnl is not None and s.mean_net_pnl <= s.mean_pnl + 1e-9
            assert s.ci_low is not None and s.ci_low <= s.mean_net_pnl <= s.ci_high  # type: ignore[operator]
    other = rep.sleeves[-1]
    assert other.verdict is None and other.window is None
    assert rep.sleeves[0].window == "10–29" and rep.sleeves[0].max_positions == 5
    assert rep.measurement_start == EPOCH
    assert (
        sum(s.opens for s in rep.sleeves)
        == conn.execute("SELECT COUNT(*) FROM open_structures").fetchone()[0]
    )
    text = "\n".join(sleeve_report_lines(rep))
    assert "short 10–29 DTE: opens" in text and "swing 30–60 DTE" in text
    assert "other (entry DTE fits no sleeve)" in text
    assert "rule (D97)" in text


def test_sleeve_report_window_and_excursions(layout: SimpleNamespace, conn) -> None:  # noqa: ANN001
    rep = sleeve_report(conn, SLEEVES, since=EPOCH, until=NOW, now=NOW)
    swing = next(s for s in rep.sleeves if s.sleeve == "swing")
    pre = closed_positions(conn, start=EPOCH, end=NOW, now=NOW)
    assert layout.pre not in {c.structure_id for c in pre}
    assert sum(s.closed for s in rep.sleeves) == len(pre)
    assert swing.closed == sum(1 for c in pre if layout.sleeve.get(c.structure_id) == "swing")
    # MAE / MFE only where an outcome row recorded them (n_excursion counts those)
    for s in rep.sleeves:
        assert s.n_excursion <= s.closed
        assert (s.mean_mae is None) == (s.n_excursion == 0)


@pytest.mark.parametrize(
    ("n", "mean", "ci", "weeks", "expected"),
    [
        (MIN_CLOSED - 1, 10.0, (1.0, 20.0), 1.0, (False, "collect")),  # too early
        (MIN_CLOSED, 0.0, (-5.0, 5.0), 1.0, (True, "keep")),  # mean >= 0 keeps
        (MIN_CLOSED, -3.0, (-6.0, -0.5), 1.0, (True, "shrink")),  # CI entirely < 0
        (MIN_CLOSED, -3.0, (-6.0, 0.0), 1.0, (True, "collect")),  # CI touches 0
        (5, -3.0, (-6.0, -1.0), 6.0, (True, "shrink")),  # 6 weeks reached first
        (5, -3.0, (-6.0, -1.0), None, (False, "collect")),
        (MIN_CLOSED, None, None, 1.0, (True, "collect")),
    ],
)
def test_keep_shrink_rule(n, mean, ci, weeks, expected) -> None:  # noqa: ANN001
    assert verdict(n, mean, ci, weeks=weeks) == expected


def test_bootstrap_is_deterministic_and_brackets_the_mean() -> None:
    xs = [-50.0, -20.0, -35.0, -10.0, -60.0, -5.0, -40.0]
    a, b = bootstrap_mean_ci(xs), bootstrap_mean_ci(xs)
    assert a == b and a is not None
    assert a[0] <= sum(xs) / len(xs) <= a[1] and a[1] < 0
    assert bootstrap_mean_ci([]) is None
    assert bootstrap_mean_ci([7.0]) == (7.0, 7.0)


def test_cli_sleeves_json(layout: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    rc = main(["scorecard", "sleeves", "--json", "--db", str(layout.path)])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    names = [s["sleeve"] for s in doc["sleeves"]]
    assert names[:2] == ["short", "swing"]  # the default profile is cash_debit (D97)
    assert doc["min_closed"] == MIN_CLOSED
    rc = main(["scorecard", "sleeves", "--since", "2026-08-01", "--db", str(layout.path)])
    assert rc == 0
    assert "sleeves since 2026-08-01" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Day Recap (Slack)
# ---------------------------------------------------------------------------


def test_day_recap_lines(layout: SimpleNamespace, conn) -> None:  # noqa: ANN001
    day = max(c.closed_at for c in closed_positions(
        conn, start=EPOCH, end=NOW, now=NOW
    )).astimezone(ET).date()  # fmt: skip
    lines = day_sleeve_lines(conn, day, SLEEVES, now=NOW)
    assert lines[0].startswith("Short 10–29: ") and lines[1].startswith("Swing 30–60: ")
    assert lines[0].endswith("2/5 open") and lines[1].endswith("1/5 open")
    assert all(" opened · " in x and " closed" in x for x in lines)
    # a single-window profile keeps the recap unchanged
    single = [DteSleeve(name="default", dte_min=30, dte_max=45)]
    assert day_sleeve_lines(conn, day, single, now=NOW) == []


def test_day_recap_line_format() -> None:
    from arc.store.db import connect
    from arc.store.migrate import migrate

    c = connect(":memory:")
    migrate(c)
    assert day_sleeve_lines(c, NOW.date(), SLEEVES, now=NOW) == [
        "Short 10–29: 0 opened · 0 closed · 0/5 open",
        "Swing 30–60: 0 opened · 0 closed · 0/5 open",
    ]


def test_reconcile_recap_appends_sleeve_lines(layout: SimpleNamespace, conn) -> None:  # noqa: ANN001
    from arc.broker.reconcile_job import _day_recap

    report = SimpleNamespace(day=NOW.date(), day_pnl=120.0, baseline=SimpleNamespace(value=25000))
    ctx = SimpleNamespace(conn=conn, settings=_settings(), now=NOW)
    view = _day_recap(ctx, report)  # type: ignore[arg-type]
    assert view is not None
    lines = view.text.split("\n")
    assert lines[-2].startswith("Short 10–29: ") and lines[-1].startswith("Swing 30–60: ")
    # no settings on the context: the recap still posts, without sleeve lines
    bare = _day_recap(SimpleNamespace(conn=conn, now=NOW), report)  # type: ignore[arg-type]
    assert bare is not None and "Short 10–29" not in bare.text


# ---------------------------------------------------------------------------
# Tower
# ---------------------------------------------------------------------------


def test_meta_and_usage(conn) -> None:  # noqa: ANN001
    meta = sleeve_meta(_settings())
    assert [(m.label, m.dte_min, m.dte_max, m.max_positions) for m in meta] == [
        ("Short", 10, 29, 5),
        ("Swing", 30, 60, 5),
    ]
    short_exit = {e.kind: e.summary for e in meta[0].exits}
    assert "close at 3 DTE" in short_exit["vertical_debit"]
    assert sleeve_meta(ArcSettings(account_profile="margin")) == []
    usage = sleeve_usage(conn, SLEEVES)
    assert [(u.label, u.open, u.max_positions) for u in usage] == [("Short", 2, 5), ("Swing", 1, 5)]
    assert sleeve_usage(conn, []) == []


def test_performance_sleeve_filter(layout: SimpleNamespace, conn) -> None:  # noqa: ANN001
    every = load_performance(conn, now=NOW, preset="all", compare="none", sleeves=SLEEVES)
    by = {r.key: r for r in every.breakdowns.sleeve}
    assert set(by) == {"short", "swing", OTHER}
    assert [r.label for r in every.breakdowns.sleeve if r.key == "short"] == ["Short"]
    assert by["short"].filter == {"sleeve": "short", "stage": "closed"}
    stats = {s.sleeve: s for s in every.sleeve_stats}
    total = 0
    for name in ("short", "swing", OTHER):
        one = load_performance(
            conn, now=NOW, preset="all", compare="none", sleeve=name, sleeves=SLEEVES
        )
        assert one.sleeve == name
        assert one.win_loss.stats.closed == by[name].count == stats[name].closed
        assert one.net_pnl.source == "realised"
        assert one.net_pnl.net == pytest.approx(by[name].pnl)
        assert {r.key for r in one.breakdowns.sleeve} == {name}
        total += one.win_loss.stats.closed
        # costs only from this sleeve's fills
        assert one.costs.fills <= every.costs.fills
    assert total == every.win_loss.stats.closed
    # no sleeves (a single-window profile): no stats, everything in `other`
    plain = load_performance(conn, now=NOW, preset="all", compare="none")
    assert plain.sleeve_stats == [] and plain.sleeve is None
    bd = load_breakdown(conn, now=NOW, by="sleeve", preset="all", sleeve="swing", sleeves=SLEEVES)
    assert [r.key for r in bd.rows] == ["swing"] and bd.sleeve == "swing"


def test_trades_sleeve_filter(layout: SimpleNamespace, conn) -> None:  # noqa: ANN001
    def rows(names: list[str]):  # noqa: ANN202
        f = TradeFilters(sleeve=names, kind=["open"], stage=["closed"])
        return load_trades(conn, f, now=NOW, size=200, sleeves=SLEEVES).items

    every = rows([])
    assert every and all(r.sleeve == layout.sleeve[r.structure_id] for r in every)
    for name in ("short", "swing", OTHER):
        got = rows([name])
        assert got and all(r.sleeve == name for r in got)
    assert len(rows(["short", "swing"])) + len(rows([OTHER])) == len(every)
    pre = next(r for r in every if r.structure_id == layout.pre)
    assert pre.entry_dte == 45 and pre.sleeve == "swing"
    opts = load_filter_options(conn, now=NOW, sleeves=SLEEVES)
    assert opts.sleeves == ["short", "swing", OTHER]
    assert load_filter_options(conn, now=NOW).sleeves == []


@pytest.fixture
def client(layout: SimpleNamespace, tmp_path: Path):
    db = tmp_path / "arc.db"
    shutil.copy(layout.path, db)
    with TestClient(create_app(db, _settings(), clock=lambda: NOW)) as c:
        yield c


def test_api_routes(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert [s["label"] for s in meta["sleeves"]] == ["Short", "Swing"]
    r = client.get("/api/performance", params={"preset": "all", "sleeve": "short"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sleeve"] == "short" and len(body["sleeve_stats"]) == 3
    assert client.get("/api/performance", params={"sleeve": "nope"}).status_code == 422
    assert client.get("/api/performance", params={"sleeve": "Bad!"}).status_code == 422
    bd = client.get("/api/performance/breakdown", params={"by": "sleeve", "preset": "all"})
    assert {r["key"] for r in bd.json()["rows"]} == {"short", "swing", "other"}
    t = client.get("/api/trades", params={"sleeve": "swing", "kind": "open"}).json()
    assert t["total"] > 0 and {i["sleeve"] for i in t["items"]} == {"swing"}
    assert client.get("/api/trades", params={"sleeve": "nope"}).status_code == 422
    assert client.get("/api/trades/filters").json()["sleeves"] == ["short", "swing", "other"]
    ov = client.get("/api/overview").json()
    assert [(s["label"], s["open"], s["max_positions"]) for s in ov["sleeves"]] == [
        ("Short", 2, 5),
        ("Swing", 1, 5),
    ]
