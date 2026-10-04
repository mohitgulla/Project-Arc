"""E10.5 (D44): experiments on the tower and in Slack.

Parity (card acceptance): one fixture DB -> the same numbers in the Slack daily
line, the tower API (``/api/experiments``, ``/api/experiments/{id}``) and
``arc experiment report --stored --json``. Plus the line/card formats, the
chart series, the routine posts through the dispatcher's heartbeat, and the
read-only API (GET only, 404 on unknown ids, empty store).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path  # noqa: TC003 - pytest fixture annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from arc.experiments import stats, view
from arc.experiments.cli import add_experiment_parser, run_experiment
from arc.experiments.config import ExperimentsConfig
from arc.experiments.evaluate import ExperimentReport, build_report, evaluate, latest_report
from arc.experiments.models import ExperimentStatus
from arc.experiments.store import ExperimentStore
from arc.slack.experiments import experiment_line, experiment_stop_card, stop_title
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.api import create_app
from tests import experiment_fixtures as fx

CFG = ExperimentsConfig()


def _after(days: list[dt.date]) -> dt.datetime:
    return fx.eod(days[-1]) + dt.timedelta(minutes=15)


def _curves(n: int, edge: float, seed: int = 7) -> tuple[list[float], list[float]]:
    rng = np.random.default_rng(seed)
    ctrl = [float(x) for x in rng.normal(20, 300, n)]
    treat = [c + edge + float(e) for c, e in zip(ctrl, rng.normal(0, 120, n), strict=True)]
    return ctrl, treat


@pytest.fixture
def fixture_db(tmp_path: Path) -> tuple[Path, dt.datetime]:
    """X-2 (ab, running, evaluated after 14 sessions) and X-1 (A/A, draft)."""
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    store = fx.start(c, fx.spec("X-2"))
    ctrl, treat = _curves(14, 60.0)
    days = fx.equity_curves(c, "X-2", ctrl, treat)
    fx.executions(c, None, 3, day=days[2])
    fx.executions(c, "X-2:treatment", 4, attempts=2, day=days[3])
    now = _after(days)
    store._now = lambda: now  # noqa: SLF001
    evaluate(store, "X-2", CFG, now=now)
    store.create(fx.spec("X-1", kind="aa"), actor="local")
    c.close()
    return db, now


def _client(db: Path, now: dt.datetime) -> TestClient:
    return TestClient(create_app(db, clock=lambda: now))


def _cli(*argv: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    add_experiment_parser(p.add_subparsers(dest="command"))
    return p.parse_args(["experiment", *argv])


# ---------------------------------------------------------------------------
# Parity: Slack line == tower API == arc experiment report --json
# ---------------------------------------------------------------------------


def test_parity_slack_line_tower_api_and_cli_report(
    fixture_db: tuple[Path, dt.datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    db, now = fixture_db
    assert run_experiment(_cli("report", "X-2", "--db", str(db), "--stored", "--json")) == 0
    out = capsys.readouterr().out
    cli = json.loads(out[out.index("{\n") :])
    report = ExperimentReport.model_validate(cli)

    client = _client(db, now)
    items = {i["experiment_id"]: i for i in client.get("/api/experiments").json()["items"]}
    row = items["X-2"]
    detail = client.get("/api/experiments/X-2").json()

    # the tower serves the stored report verbatim (same canonical JSON, same hash)
    assert detail["report"] == cli
    assert detail["report_hash"] == report.report_hash()
    # list row numbers == the CLI report's
    assert row["sessions"] == cli["sessions"] == 14
    assert row["primary_mean"] == cli["primary"]["mean"]
    assert row["primary_ci_lo"] == cli["primary"]["ci"]["lo"]
    assert row["primary_ci_hi"] == cli["primary"]["ci"]["hi"]
    assert row["sortino_control"] == cli["secondary"]["sortino_control"]
    assert row["sortino_treatment"] == cli["secondary"]["sortino_treatment"]
    assert row["verdict"] == cli["verdict"] == "continue"
    assert (row["min_sessions"], row["max_sessions"]) == (20, 60)

    # the Slack line is rendered from the same numbers, and the tower carries it verbatim
    line = experiment_line(report).text
    assert row["line"] == detail["experiment"]["line"] == line
    assert row["primary_p"] == cli["primary"]["p_value"] is not None
    assert row["sortino_p"] == cli["secondary"]["p_value"] is not None
    sd = cli["secondary"]["diff_ci"]["estimate"]
    assert row["sortino_delta"] == pytest.approx(sd)
    mean, lo, hi = cli["primary"]["mean"], cli["primary"]["ci"]["lo"], cli["primary"]["ci"]["hi"]
    want = (
        f"[X-2] Day 14 • P&L ∆ {view.pct(mean)}/day (p: {view.p_text(cli['primary']['p_value'])})"
        f" • Sortino ∆ {view.ratio(sd, sign=True)} (p: {view.p_text(cli['secondary']['p_value'])})"
    )
    assert line == want

    # per-arm table: same drawdown / worst day / orders as the CLI report
    arms = {a["arm"]: a for a in detail["report"]["arms"]}
    assert arms["control"]["orders"] == 3 and arms["treatment"]["orders"] == 8
    # the equity curves end at t0 equity + each arm's total P&L
    assert detail["curves"][0] == {"day": None, "control": 100_000.0, "treatment": 100_000.0}
    last = detail["curves"][-1]
    assert last["control"] == pytest.approx(100_000.0 + arms["control"]["total_pnl"])
    assert last["treatment"] == pytest.approx(100_000.0 + arms["treatment"]["total_pnl"])
    # the cumulative band's last point is the report's CI x sessions
    cum = detail["cumulative"][-1]
    assert cum["n"] == 14
    assert cum["cum_d"] == pytest.approx(mean * 14)
    assert cum["lo"] == pytest.approx(lo * 14) and cum["hi"] == pytest.approx(hi * 14)


def test_unevaluated_experiment_shows_spec_only(fixture_db: tuple[Path, dt.datetime]) -> None:
    db, now = fixture_db
    client = _client(db, now)
    items = client.get("/api/experiments").json()["items"]
    assert [i["experiment_id"] for i in items] == ["X-1", "X-2"]  # newest first
    x1 = items[0]
    assert x1["status"] == "draft" and x1["kind"] == "aa" and x1["sessions"] is None
    assert x1["line"] is None and x1["secondary"] is None
    d = client.get("/api/experiments/X-1").json()
    assert d["report"] is None and d["curves"] == [] and d["cumulative"] == []
    assert d["registered_hash"] is None and d["spec"]["id"] == "X-1"


def test_detail_carries_spec_prereg_hash_and_shas(fixture_db: tuple[Path, dt.datetime]) -> None:
    db, now = fixture_db
    d = _client(db, now).get("/api/experiments/X-2").json()
    assert d["registered_hash"] == d["spec_hash"] == d["report"]["spec_hash"]
    assert len(d["spec_hash"]) == 64
    assert d["running"]["control_sha"] == fx.CONTROL_SHA
    assert d["report"]["control_sha"] == fx.CONTROL_SHA
    overlay = d["spec"]["arms"]["treatment"]["overlay"]
    assert overlay == {"exits": {"default": {"take_profit_pct": 0.4}}}
    assert [e["status"] for e in d["events"]] == ["draft", "registered", "running"]


def test_api_unknown_id_404_get_only_and_empty_store(
    fixture_db: tuple[Path, dt.datetime], tmp_path: Path
) -> None:
    db, now = fixture_db
    client = _client(db, now)
    r = client.get("/api/experiments/X-99")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert client.post("/api/experiments").status_code == 405
    empty = tmp_path / "empty.db"
    c = connect(empty)
    migrate(c)
    c.close()
    body = _client(empty, now).get("/api/experiments").json()
    assert body["items"] == [] and body["as_of"]


def test_tower_experiments_module_does_not_load_llm_routing() -> None:
    """The lint-imports ignore for evaluate -> manifest relies on that import being lazy."""
    import subprocess
    import sys

    code = (
        "import sys, arc.tower.data_experiments, arc.tower.routes.experiments;"
        "assert 'arc.llm_routing' not in sys.modules;"
        "assert 'arc.routines.manifest' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603


def test_tower_never_writes(fixture_db: tuple[Path, dt.datetime]) -> None:
    db, now = fixture_db
    c = sqlite3.connect(db)
    before = c.execute("SELECT count(*) FROM experiment_reports").fetchone()[0]
    c.close()
    client = _client(db, now)
    for _ in range(2):
        assert client.get("/api/experiments").status_code == 200
        assert client.get("/api/experiments/X-2").status_code == 200
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM experiment_reports").fetchone()[0] == before
    c.close()


# ---------------------------------------------------------------------------
# view: formats and series
# ---------------------------------------------------------------------------


def test_pct_uses_real_minus_and_na() -> None:
    assert view.pct(0.0008) == "+0.08%"
    assert view.pct(-0.0003) == "−0.03%"
    assert view.pct(-0.0003, unit="") == "−0.03"
    assert view.pct(0.0123, 3, sign=False) == "1.230%"
    assert view.pct(None) == "n/a"


def _report(
    conn: sqlite3.Connection, ctrl: list[float], treat: list[float], **kw: str
) -> ExperimentReport:
    store = fx.start(conn, fx.spec("X-2", **kw))
    days = fx.equity_curves(conn, "X-2", ctrl, treat)
    return build_report(conn, store.require("X-2"), CFG, now=_after(days))


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def test_daily_line_matches_owner_format(conn: sqlite3.Connection) -> None:
    ctrl, treat = _curves(14, 80.0)
    r = _report(conn, ctrl, treat)
    line = view.daily_line(r)
    head, pnl, sortino = line.split(" • ")
    assert head == "[X-2] Day 14"
    assert re.fullmatch(r"P&L ∆ [+−]\d+\.\d\d%/day \(p: (<0\.001|\d\.\d{2,3})\)", pnl), pnl
    assert re.fullmatch(r"Sortino ∆ [+−]\d+\.\d\d \(p: (<0\.001|\d\.\d{2,3})\)", sortino), sortino
    assert "-" not in pnl + sortino  # real minus signs only
    assert "[Experiments]" not in line and " day " not in line


def test_line_before_any_ci_and_for_aa(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("X-2"))
    r0 = build_report(conn, store.require("X-2"), CFG, now=fx.T0)
    assert view.daily_line(r0) == "[X-2] Day 0 • P&L ∆ n/a (p: n/a) • Sortino ∆ n/a (p: n/a)"
    c2 = connect(":memory:")
    migrate(c2)
    aa = _report(c2, [10.0, -5.0, 3.0], [12.0, -4.0, 1.0], kind="aa")
    assert view.daily_line(aa).startswith("[X-2] A/A Day 3 • P&L ∆ ")


def test_msprt_p_is_the_dual_of_the_ci(conn: sqlite3.Connection) -> None:
    """p < alpha exactly when the always-valid CI excludes 0 (same sigma, tau)."""
    for seed, edge in [(1, 0.0), (2, 40.0), (3, 120.0), (5, 400.0), (9, -300.0)]:
        c = connect(":memory:")
        migrate(c)
        ctrl, treat = _curves(25, edge, seed=seed)
        r = _report(c, ctrl, treat)
        assert r.primary.ci is not None and r.primary.p_value is not None
        assert (r.primary.p_value < r.alpha) == r.primary.ci.excludes_zero, (seed, edge)


def test_sortino_p_agrees_with_non_inferiority(conn: sqlite3.Connection) -> None:
    for seed, edge in [(1, 0.0), (5, 400.0), (9, -300.0)]:
        c = connect(":memory:")
        migrate(c)
        ctrl, treat = _curves(25, edge, seed=seed)
        r = _report(c, ctrl, treat)
        s = r.secondary
        assert s.p_value is not None and s.non_inferior is not None
        # one-sided bootstrap p and the level 1-2a percentile CI share the resamples
        assert (s.p_value < r.alpha) == s.non_inferior or abs(s.p_value - r.alpha) < 0.01


def test_p_text() -> None:
    assert view.p_text(None) == "n/a"
    assert view.p_text(0.0004) == "<0.001"
    assert view.p_text(0.004) == "0.004"
    assert view.p_text(0.214) == "0.21"


def test_cumulative_band_matches_confidence_sequence_at_every_prefix(
    conn: sqlite3.Connection,
) -> None:
    ctrl, treat = _curves(10, 50.0, seed=3)
    r = _report(conn, ctrl, treat)
    pts = view.cumulative(r, mde=None)
    d = [row.d for row in r.series]
    assert [p.n for p in pts] == list(range(1, 11))
    for p in pts:
        ci, _, _ = stats.confidence_sequence(
            d[: p.n], alpha=r.alpha, sigma=None, sigma_upper_q=0.05, mde=None, min_sessions=20
        )
        assert p.cum_d == pytest.approx(sum(d[: p.n]))
        if ci is None:
            assert p.lo is None and p.hi is None
        else:
            assert p.lo == pytest.approx(ci.lo * p.n) and p.hi == pytest.approx(ci.hi * p.n)
    assert pts[0].lo is None  # one session: no spread, no CI


def test_cumulative_uses_the_aa_sigma_when_the_report_did(conn: sqlite3.Connection) -> None:
    ctrl, treat = _curves(6, 50.0, seed=4)
    store = fx.start(conn, fx.spec("X-2"))
    days = fx.equity_curves(conn, "X-2", ctrl, treat)
    r = build_report(conn, store.require("X-2"), CFG, now=_after(days), aa_sigma=0.004)
    assert r.primary.sigma_source == "aa"
    last = view.cumulative(r, mde=None)[-1]
    assert r.primary.ci is not None
    assert last.lo == pytest.approx(r.primary.ci.lo * 6)
    assert last.hi == pytest.approx(r.primary.ci.hi * 6)


# ---------------------------------------------------------------------------
# Slack stop card
# ---------------------------------------------------------------------------


def _stopped_report(conn: sqlite3.Connection) -> ExperimentReport:
    ctrl, treat = _curves(25, 400.0, seed=5)
    store = fx.start(conn, fx.spec("X-2"))
    days = fx.equity_curves(conn, "X-2", ctrl, treat)
    store._now = lambda: _after(days)  # noqa: SLF001
    rep = evaluate(store, "X-2", CFG, now=_after(days))
    assert rep.verdict == "win"
    assert store.require("X-2").status is ExperimentStatus.STOPPED
    return rep


def _texts(blocks: list[dict]) -> str:  # type: ignore[type-arg]
    return json.dumps(blocks, ensure_ascii=False)


def test_stop_card_layout_matches_owner_spec(conn: sqlite3.Connection) -> None:
    rep = _stopped_report(conn)
    card = experiment_stop_card(rep)
    b = card.blocks
    assert b[0]["type"] == "header"
    title = b[0]["text"]["text"]
    assert title == stop_title(rep) == card.text
    assert title == (f"[X-2] Day 25 • Win • {view.delta_text(rep)} • {view.sortino_text(rep)}")
    fields = [f["text"] for blk in b if blk.get("fields") for f in blk["fields"]]
    labels = [f.split("\n", 1)[0] for f in fields]
    assert labels[:5] == [
        "*Paired Daily P&L*",
        "*Sortino Ratio*",
        "*Control*",
        "*Treatment*",
        "*Sessions*",
    ]
    prim, sec, ctrl, treat, sess = fields[:5]
    # primary and secondary share one shape: ∆, p, CI, control, treatment, margin
    for f, p_label in ((prim, "mSPRT p "), (sec, "Bootstrap p ")):
        rows = [ln.split(" ", 1)[0] for ln in f.split("\n")[1:]]
        assert rows[:6] == ["∆", p_label.split()[0], rows[2], "Control", "Treatment", "Margin"]
        assert p_label in f and " CI [" in f
    assert "Margin −0.50 (non-inferiority)" in sec
    for arm in (ctrl, treat):
        for k in ("P&L ", "Max drawdown", "Worst day", "Orders", "Fills", "Mean slippage"):
            assert k in arm
    assert f"Day 25/{rep.min_sessions}–{rep.max_sessions}" in sess
    assert f"{rep.t0:%b %-d} (t0) • Equity $100,000" in sess
    whole = _texts(b)
    assert "Tower report" not in whole and "*Report*" not in whole
    assert "arc experiment report" not in whole
    reason = next(
        blk for blk in b if blk.get("text", {}).get("text", "").startswith("*Verdict reason*")
    )
    bullets = reason["text"]["text"].split("\n")[1:]
    assert len(bullets) == 2
    assert bullets[0].startswith("• Primary CI [") and "&gt; 0" in bullets[0]  # mrkdwn-escaped
    assert bullets[1].startswith("• Secondary CI [") and "non-inferior" in bullets[1]
    assert b[-1]["type"] == "context" and f"report `{rep.report_hash()[:12]}`" in whole
    assert "```" not in whole  # no code blocks on persona cards


def test_stop_card_for_invalid_aa(conn: sqlite3.Connection) -> None:
    store = fx.start(conn, fx.spec("X-1", kind="aa"))
    days = fx.equity_curves(conn, "X-1", [0.0] * 10, [300.0 + (i % 3) * 10 for i in range(10)])
    store._now = lambda: _after(days)  # noqa: SLF001
    rep = evaluate(store, "X-1", CFG, now=_after(days))
    card = experiment_stop_card(rep)
    assert card.blocks[0]["text"]["text"].startswith("[X-1] A/A Day 10 • Invalid • P&L ∆ ")
    whole = _texts(card.blocks)
    assert "Margin n/a (A/A)" in whole and "A/A: no margin" in whole


# ---------------------------------------------------------------------------
# Routine -> dispatcher -> day thread
# ---------------------------------------------------------------------------


def test_routine_posts_line_and_stop_card_through_dispatcher(
    fixture_db: tuple[Path, dt.datetime],
) -> None:
    """The real handler on the tick: quiet [Routines] summary, then a line per running
    experiment and a stop card per stopped one, each its own day-thread post."""
    from arc.routines.config import RoutinesConfig
    from arc.routines.dispatcher import Dispatcher
    from arc.routines.experiments import experiments_evaluate_step
    from arc.routines.heartbeat import RecordingNotifier

    db, now = fixture_db
    c = connect(db)
    # a second running experiment in another area that wins on this evaluation
    store = fx.start(c, fx.spec("X-3", area="ranking"))
    ctrl, treat = _curves(25, 400.0, seed=5)
    days = fx.equity_curves(c, "X-3", ctrl, treat, prior_close=None)
    del days
    cfg = RoutinesConfig.model_validate(
        {
            "personas": {
                "experiments.evaluate": {
                    "schedule": ["16:40"],
                    "days": "trading",
                    "llm": False,
                    "notify": "quiet",
                }
            }
        }
    )
    notes = RecordingNotifier()
    d = Dispatcher(
        c,
        cfg,
        handlers={"experiments.evaluate": experiments_evaluate_step},
        notifier=notes,
        is_halted=lambda: False,
    )
    tick = dt.datetime.combine(fx.sessions(25)[-1], dt.time(16, 40), tzinfo=fx.ET)
    d.tick(tick, since=tick - dt.timedelta(minutes=5))
    posts = notes.day_thread_posts()
    assert len(posts) == 2, posts
    assert posts[0].startswith("[X-2] Day 14 • P&L ∆ ")
    assert posts[1].startswith("[X-3] Day 25 • Win • P&L ∆ ")
    assert notes.blocks[0] is None and notes.blocks[1] is not None
    assert store.require("X-3").status is ExperimentStatus.STOPPED
    c.close()


def test_heartbeat_card_posts_verbatim(conn: sqlite3.Connection) -> None:
    from arc.routines.heartbeat import Heartbeats, RecordingNotifier

    rec = RecordingNotifier()
    hb = Heartbeats(conn, rec)
    ctrl, treat = _curves(5, 50.0)
    r = _report(conn, ctrl, treat)
    line = experiment_line(r)
    hb.card(fx.eod(fx.T0.date()), line.text, line.blocks or None)
    assert rec.day_thread_posts() == [line.text]
    assert rec.blocks == [None]


def test_latest_report_is_what_the_tower_serves_after_a_second_evaluation(
    fixture_db: tuple[Path, dt.datetime],
) -> None:
    db, now = fixture_db
    c = connect(db)
    store = ExperimentStore(c, now=lambda: now)
    later = now + dt.timedelta(minutes=5)
    rep2 = evaluate(store, "X-2", CFG, now=later)
    c.close()
    d = _client(db, later).get("/api/experiments/X-2").json()
    assert d["report"]["evaluated_at"] == rep2.model_dump(mode="json")["evaluated_at"]
    ro = connect(db)
    stored = latest_report(ro, "X-2")
    ro.close()
    assert stored is not None and d["report_hash"] == stored.report_hash()
