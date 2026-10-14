"""E25.6 (D108, D109): production naming on Slack and the Tower for spec v3 experiments.

The daily status (title, bold-italic headline, one ``Ti − C`` line per treatment, sent
to #arc-investor too), the stop card (Start book, Control/T1..T3, Production block) and
the Tower detail payload (regime panel, Production label in the scope docs).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import numpy as np
from fastapi.testclient import TestClient

from arc.experiments import view
from arc.slack.experiments import experiment_line, experiment_stop_card
from arc.slack.loop import RECAP_MAX
from tests import experiment_fixtures as fx
from tests.test_experiment_production import BOOK, _book, _conn, _k3_scenario, _report, _v3

if TYPE_CHECKING:
    from arc.experiments.evaluate import ExperimentReport

SLEEVE = re.compile(r"\b(short|swing)\b", re.IGNORECASE)


def _k3() -> ExperimentReport:
    c = _conn()
    fx.start(c, _v3(max_sessions=40), legacy=[BOOK])
    return _report(c, "XP-30", _k3_scenario(c))


def _k3_open() -> ExperimentReport:
    """K = 3, day 14: no arm decided; T2 has the largest gap."""
    c = _conn()
    fx.start(c, _v3(max_sessions=40), legacy=[BOOK])
    rng = np.random.default_rng(3)
    n = 14
    ctrl = list(rng.normal(20, 300, n))
    noise = rng.normal(0, 100, (3, n))
    treats = {
        "t1": [x + 5 + e for x, e in zip(ctrl, noise[0], strict=True)],
        "t2": [x + 60 + e for x, e in zip(ctrl, noise[1], strict=True)],
        "t3": [x + e for x, e in zip(ctrl, noise[2], strict=True)],
    }
    marks: list[float | None] = [500.0] * n
    days = _book(c, "XP-30", p=[50.0] * n, ctrl=ctrl, treats=treats, book_marks=marks)
    return _report(c, "XP-30", days)


GAP_LINE = re.compile(
    r"T[123] − C • P&L ∆ [+−]\d+\.\d\d%/day \(p (<0\.001|\d\.\d{2,3}) Holm\)"
    r" • Sortino ∆ [+−]\d+\.\d\d \(p (<0\.001|\d\.\d{2,3}|n/a)\)"
)


def test_daily_status_k3_title_headline_and_three_ti_minus_c_lines() -> None:
    r = _k3_open()
    assert r.verdict == "continue" and r.reference == "control"
    card = experiment_line(r)
    assert card.broadcast is True  # "Also send to #arc-investor" (D65)
    assert card.blocks == []
    lines = card.text.split("\n")
    assert lines[0] == "[XP-30] Day 14/40"
    head = lines[1]
    assert head.startswith("_*") and head.endswith("*_")
    assert len(head) - 4 <= RECAP_MAX
    assert head == (
        "_*T2 leads control by +0.05%/day (p 1.00 Holm); T1 and T3 within noise"
        " after 14 sessions.*_"
    )
    assert [ln.split(" ", 1)[0] for ln in lines[2:]] == ["T1", "T2", "T3"]
    for ln in lines[2:]:
        assert GAP_LINE.fullmatch(ln), ln
    # the gap lines carry the report's Holm-adjusted numbers
    by = {p.b: p for p in r.pairwise or [] if p.kind == "control_vs_treatment"}
    assert (
        f"P&L ∆ {view.pct(by['t2'].mean)}/day (p {view.p_text(by['t2'].holm_p)} Holm)" in lines[3]
    )


def test_daily_status_has_no_production_or_sleeve_text() -> None:
    for r in (_k3(), _k3_open()):
        text = experiment_line(r).text
        assert "Production" not in text and "production" not in text
        assert " − P " not in text and "Ti − P" not in text
        assert "start book" not in text.lower() and "residual" not in text
        assert not SLEEVE.search(text), text
        assert "Arc Investor" not in text


def test_daily_status_decided_arms_lead_the_headline() -> None:
    r = _k3()
    head = view.status_headline(r)
    assert head.startswith("T1 beats control by +0.39%/day (p <0.001 Holm); T2 trails control by")
    assert head.endswith(" after 30 sessions.")
    assert len(head) <= RECAP_MAX


def test_headline_never_drops_the_session_count_when_long() -> None:
    r = _k3()
    pairs = [
        p.model_copy(update={"b": f"t{i + 1}"})
        for i, p in enumerate([q for q in r.pairwise or [] if q.kind == "control_vs_treatment"] * 4)
    ]
    long = r.model_copy(update={"pairwise": pairs})
    head = view.status_headline(long)
    assert len(head) <= RECAP_MAX and head.endswith(" after 30 sessions.")


def test_stop_card_v3_start_book_arms_and_production_block() -> None:
    r = _k3()
    card = experiment_stop_card(r)
    assert card.broadcast is True
    b = card.blocks
    assert b[0]["type"] == "header"
    assert b[1]["type"] == "section" and b[1]["text"]["text"].startswith("_*T1 beats control")
    fields = [f["text"] for blk in b if blk.get("fields") for f in blk["fields"]]
    labels = [f.split("\n", 1)[0] for f in fields]
    assert labels == [
        "*Paired Daily P&L*",
        "*Sortino Ratio*",
        "*Control*",
        "*T1*",
        "*T2*",
        "*T3*",
        "*Production*",
        "*Sessions*",
        "*Calibration*",
    ]
    prod = fields[labels.index("*Production*")].split("\n")[1:]
    pp = r.production
    assert pp is not None
    assert prod[0].startswith("P&L ") and view.pct(pp.cum_p_pnl / r.t0_equity) in prod[0]
    assert prod[1] == f"P − C {view.pct(pp.cum_d_p_c)}"
    assert prod[2] == f"= start book {view.pct(pp.cum_start_book)}"
    assert prod[3] == f"+ residual {view.pct(pp.cum_residual)}"
    assert prod[4] == "Start book 1/1 open"
    sess = fields[labels.index("*Sessions*")]
    assert "Start book 1" in sess
    whole = json.dumps(b, ensure_ascii=False)
    assert "Legacy book" not in whole and "Arc Investor" not in whole
    assert not SLEEVE.search(whole)


def test_tower_detail_carries_the_regime_panel_for_v3(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import datetime as dt

    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.evaluate import evaluate
    from arc.experiments.store import ExperimentStore
    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.tower.api import create_app

    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    fx.start(c, _v3(max_sessions=40), legacy=[BOOK])
    days = _k3_scenario(c)
    now = fx.eod(days[-1]) + dt.timedelta(minutes=15)
    evaluate(ExperimentStore(c, now=lambda: now), "XP-30", ExperimentsConfig(), now=now)
    c.close()
    d = TestClient(create_app(db, clock=lambda: now)).get("/api/experiments/XP-30").json()
    assert d["report"]["reference"] == "control"
    panel = d["regime_panel"]
    assert panel["xps"] == ["XP-30"] and panel["descriptive"] is True
    assert panel["sessions"] == len(days)
    line = d["experiment"]["line"]
    assert line.split("\n")[0] == "[XP-30] Day 30/40" and "T3 − C" in line
