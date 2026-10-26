"""E24.2 (D101): missed-open retry — a `missed` dedupe state, one same-day re-admit.

An approved open whose ladder ended ``cancelled`` with nothing filled for a no-fill
reason is ``missed``: held ``dedupe.missed_retry_after`` (30m) after the ladder
finished, then re-admitted once (``dedupe.missed_retry_max``, 1; 0 = rollback).
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

import pytest

from arc.budget import Tier
from arc.config import ArcSettings
from arc.context.ttl import to_db
from arc.control.registry import REGISTRY, TunableError, parse_value
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.models import LegIntent, Stance
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.dedupe import (
    DedupeConfig,
    IdeaFingerprint,
    MissedRetry,
    RecentIdea,
    Suppression,
    check_idea,
    is_missed_detail,
    recent_ideas,
    structure_label,
)
from arc.pipeline.market import price_structure
from arc.pipeline.steps import MAX_RETRY_LINES, _held_and_recent, _retry_lines
from arc.utils.calendar import ET
from tests.test_e59_research_portfolio import _codes, _outcome, _run, _settings

if TYPE_CHECKING:
    import sqlite3

NO_FILL = "no fill after 4 attempt(s); last +11.27"
BUDGET_STOP = "order budget exhausted (40 of 40 open orders used today)"
STALE = (
    "stale band: quotes 95s old (> 60s), mid +5.10 outside the gate band +4.65 .. +4.75; not sent"
)
MOVED = (
    "market moved past the band: mid +4.85 beyond worst price +4.75 (band +4.65 .. +4.75); "
    "2 step(s) not sent; no re-gate left"
)
REGATE_FAILED = (
    "market moved past the band: mid +4.85 beyond worst price +4.75 (band +4.65 .. +4.75); "
    "2 step(s) not sent; re-gate failed: gate:quote_stale"
)
REGATE_EMPTY = "re-gated band +4.82 .. +4.83 holds no valid price"


@pytest.fixture
def routines():  # noqa: ANN201
    return _routines()


def _miss(
    conn: sqlite3.Connection,
    phash: str,
    *,
    finished: dt.datetime,
    detail: str = NO_FILL,
    status: str = "cancelled",
    filled: int = 0,
    approved: bool = True,
) -> None:
    """The approval + ladder rows of one executed (here: missed) open."""
    conn.execute(
        "INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status, "
        "decided_by, channel, created_at, expires_at, decided_at) VALUES "
        "(?, 'SPY', ?, '{}', ?, 'arc:auto-approve', 'log', ?, ?, ?)",
        (
            phash,
            finished.date().isoformat(),
            "approved" if approved else "pending",
            to_db(finished),
            to_db(finished),
            to_db(finished),
        ),
    )
    conn.execute(
        "INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo, band_hi, "
        "max_steps, contracts, filled_qty, detail, started_at, finished_at) VALUES "
        "(?, 'open', ?, 'arc2', '1.00', '1.10', 3, 1, ?, ?, ?, ?)",
        (
            phash,
            status,
            filled,
            detail,
            to_db(finished - dt.timedelta(minutes=3)),
            to_db(finished),
        ),
    )
    conn.commit()


def _open_hashes(conn: sqlite3.Connection) -> list[str]:
    return [
        r["proposal_hash"]
        for r in conn.execute(
            "SELECT proposal_hash FROM proposals WHERE kind='open' ORDER BY rowid"
        )
    ]


def _fp() -> IdeaFingerprint:
    return IdeaFingerprint(
        ticker="TSLA", stance=Stance.BEARISH, structure_type="vertical_spread",
        expiry_week="2026-W47", strike_bucket=95,
    )  # fmt: skip


def _missed_prior(status: str, *, used: int = 0, mx: int = 1) -> RecentIdea:
    at = dt.datetime(2026, 10, 12, 13, 12, tzinfo=ET)
    done = dt.datetime(2026, 10, 12, 13, 16, tzinfo=ET)
    return RecentIdea(
        fingerprint=_fp().key(), kind=Suppression.MISSED, at=at, spot=D("378.6"),
        regime="transitional", ref="905a4eb62d29",
        retry=MissedRetry(
            status=status,  # type: ignore[arg-type]
            missed_at=done, ready_at=done + dt.timedelta(minutes=30), used=used, max=mx,
        ),
        label="put debit",
    )  # fmt: skip


# ---------------------------------------------------------------------------
# The no-fill classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detail", "missed"),
    [
        (NO_FILL, True),
        (
            "no fill after 1 attempt(s); last +4.25; re-priced once: 4.65–4.75 → 4.82–4.95, missed",
            True,
        ),
        (STALE, True),
        (MOVED, True),
        (REGATE_FAILED, True),
        (REGATE_EMPTY, True),
        (BUDGET_STOP, False),
        ("shared account order cap reached (200 of 200 option orders on the account today)", False),
        ("halted: owner halt", False),
        ("submit failed: 422 insufficient buying power", False),
        ("", False),
        (None, False),
    ],
)
def test_missed_detail_is_a_no_fill_reason_only(detail: str | None, missed: bool) -> None:
    assert is_missed_detail(detail) is missed


# ---------------------------------------------------------------------------
# Pure verdicts
# ---------------------------------------------------------------------------


class TestCheckIdea:
    def test_wait_is_held_retry_is_admitted_spent_is_proposed(self) -> None:
        cfg = DedupeConfig()
        wait = check_idea(
            _fp(), [_missed_prior("wait")], spot=D("378.6"), regime="transitional", cfg=cfg
        )
        assert wait.suppressed and wait.reason_code is ReasonCode.DEDUPE_MISSED_WAIT
        assert "retry from 13:46 ET" in wait.detail
        due = check_idea(
            _fp(), [_missed_prior("retry")], spot=D("378.6"), regime="transitional", cfg=cfg
        )
        assert not due.suppressed and due.reason_code is ReasonCode.DEDUPE_MISSED_RETRY
        assert due.detail.startswith("retry 1/1: missed 13:16 ET")
        spent = check_idea(
            _fp(), [_missed_prior("spent", used=1)], spot=D("378.6"), regime="transitional", cfg=cfg
        )
        assert spent.suppressed and spent.reason_code is ReasonCode.DEDUPE_PROPOSED

    def test_spot_move_still_readmits_while_waiting(self) -> None:
        v = check_idea(
            _fp(), [_missed_prior("wait")], spot=D("390"), regime="transitional", cfg=DedupeConfig()
        )
        assert not v.suppressed and v.override == "spot_moved"
        assert v.reason_code is ReasonCode.DEDUPE_OVERRIDE

    def test_proposed_outranks_missed(self) -> None:
        later = RecentIdea(
            fingerprint=_fp().key(), kind=Suppression.PROPOSED,
            at=dt.datetime(2026, 10, 12, 14, 0, tzinfo=ET), spot=D("378.6"), regime="transitional",
        )  # fmt: skip
        v = check_idea(
            _fp(), [_missed_prior("retry"), later], spot=D("378.6"), regime="transitional",
            cfg=DedupeConfig(),
        )  # fmt: skip
        assert v.suppressed and v.kind is Suppression.PROPOSED

    def test_retry_line_format(self) -> None:
        assert _missed_prior("retry").retry_line() == (
            "Retry: TSLA bearish put debit (missed 13:16 ET, 1/1)"
        )

    def test_structure_labels(self) -> None:
        env = PipelineEnv.fixtures()

        def label(legs: list[tuple[str, str]]) -> str:
            st = price_structure(
                env.market,
                [(o, LegIntent(s), 1) for o, s in legs],
                as_of=FIXTURE_NOW.date(),
                r=0.04,
            ).structure
            return structure_label(st)

        assert label([("SPY261030C00775000", "long")]) == "long call"
        assert label([("SPY261030C00775000", "long"), ("SPY261030C00785000", "short")]) == (
            "call debit"
        )
        assert label([("SPY261030P00745000", "short"), ("SPY261030P00740000", "long")]) == (
            "put credit"
        )


# ---------------------------------------------------------------------------
# recent_ideas over the audit DB (fixture pipeline day)
# ---------------------------------------------------------------------------


class TestRecentIdeas:
    def test_missed_is_held_then_readmitted_after_30m(self, routines) -> None:  # noqa: ANN001
        settings = _settings()
        conn, first = _run(settings, routines, PipelineEnv.fixtures())
        (p,) = first.proposals
        done = FIXTURE_NOW + dt.timedelta(minutes=4)
        _miss(conn, p["proposal_hash"], finished=done)
        cfg = DedupeConfig.from_settings(settings)
        (r,) = recent_ideas(conn, now=done + dt.timedelta(minutes=10), cfg=cfg)
        assert r.kind is Suppression.MISSED and r.retry is not None
        assert r.retry.status == "wait" and r.retry.ready_at == done + dt.timedelta(minutes=30)
        assert r.label == "iron condor"
        (r,) = recent_ideas(conn, now=done + dt.timedelta(minutes=30), cfg=cfg)
        assert r.retry is not None and r.retry.status == "retry"

        # propose stage: held at +10m, re-admitted at +31m (journalled both ways)
        _, held = _run(
            settings, routines, PipelineEnv.fixtures(), conn=conn,
            now=done + dt.timedelta(minutes=10),
        )  # fmt: skip
        po = _outcome(held, "quant.propose")
        assert po.metrics["proposals"] == 0 and po.metrics["dedupe"] == 1
        assert _codes(conn, "propose")["dedupe:missed_wait"] == ["SPY"]
        assert _codes(conn, "shortlist")["dedupe:missed_wait"] == ["SPY"]
        _, again = _run(
            settings, routines, PipelineEnv.fixtures(), conn=conn,
            now=done + dt.timedelta(minutes=31),
        )  # fmt: skip
        assert _outcome(again, "quant.propose").metrics["proposals"] == 1
        assert _codes(conn, "propose")["dedupe:missed_retry"] == ["SPY"]
        assert _codes(conn, "shortlist")["dedupe:missed_retry"] == ["SPY"]
        # the Research call saw the code-built Retry line
        row = conn.execute(
            "SELECT prompt_text, prompt_inputs FROM persona_calls WHERE persona='research' "
            "ORDER BY rowid DESC"
        ).fetchone()
        line = "Retry: SPY neutral iron condor (missed 16:04 ET, 1/1)"
        assert line in row["prompt_text"]
        assert json.loads(row["prompt_inputs"])["missed_retry"] == line

        # the retry is proposed again: held for the rest of the day, missed or not
        (_, second) = _open_hashes(conn)
        later = done + dt.timedelta(minutes=40)
        _miss(conn, second, finished=later)
        (r,) = recent_ideas(conn, now=later + dt.timedelta(hours=2), cfg=cfg)
        assert r.retry is not None and r.retry.status == "spent" and r.retry.used == 1
        _, third = _run(
            settings, routines, PipelineEnv.fixtures(), conn=conn,
            now=later + dt.timedelta(minutes=45),
        )  # fmt: skip
        po = _outcome(third, "quant.propose")
        assert po.metrics["proposals"] == 0 and po.metrics["dedupe"] == 1
        assert _codes(conn, "propose")["dedupe_proposed"] == ["SPY"]

    def test_pending_retry_proposal_is_proposed_not_a_second_retry(self) -> None:
        settings = _settings()
        conn, first = _run(settings, _routines(), PipelineEnv.fixtures())
        (p,) = first.proposals
        done = FIXTURE_NOW + dt.timedelta(minutes=4)
        _miss(conn, p["proposal_hash"], finished=done)
        _run(
            settings,
            _routines(),
            PipelineEnv.fixtures(),
            conn=conn,
            now=done + dt.timedelta(minutes=31),
        )
        cfg = DedupeConfig.from_settings(settings)
        kinds = sorted(
            r.kind.value for r in recent_ideas(conn, now=done + dt.timedelta(minutes=45), cfg=cfg)
        )
        assert kinds == ["missed", "proposed"]
        v = check_idea(
            IdeaFingerprint.parse(recent_ideas(conn, now=done, cfg=cfg)[0].fingerprint),
            recent_ideas(conn, now=done + dt.timedelta(minutes=45), cfg=cfg),
            spot=None, regime=None, cfg=cfg,
        )  # fmt: skip
        assert v.suppressed and v.kind is Suppression.PROPOSED

    @pytest.mark.parametrize("detail", [BUDGET_STOP, "submit failed: 500", "halted: owner halt"])
    def test_budget_stop_or_halt_cancel_is_not_missed(self, detail: str) -> None:
        settings = _settings()
        conn, first = _run(settings, _routines(), PipelineEnv.fixtures())
        (p,) = first.proposals
        done = FIXTURE_NOW + dt.timedelta(minutes=4)
        _miss(conn, p["proposal_hash"], finished=done, detail=detail)
        (r,) = recent_ideas(
            conn, now=done + dt.timedelta(hours=1), cfg=DedupeConfig.from_settings(settings)
        )
        assert r.kind is Suppression.PROPOSED and r.retry is None

    @pytest.mark.parametrize("detail", [STALE, MOVED, REGATE_FAILED, REGATE_EMPTY])
    def test_stale_or_moved_band_is_missed(self, detail: str) -> None:
        settings = _settings()
        conn, first = _run(settings, _routines(), PipelineEnv.fixtures())
        (p,) = first.proposals
        done = FIXTURE_NOW + dt.timedelta(minutes=4)
        _miss(conn, p["proposal_hash"], finished=done, detail=detail)
        (r,) = recent_ideas(
            conn, now=done + dt.timedelta(hours=1), cfg=DedupeConfig.from_settings(settings)
        )
        assert r.kind is Suppression.MISSED

    def test_filled_regate_is_executed_not_missed(self) -> None:
        settings = _settings()
        conn, first = _run(settings, _routines(), PipelineEnv.fixtures())
        (p,) = first.proposals
        done = FIXTURE_NOW + dt.timedelta(minutes=4)
        _miss(
            conn, p["proposal_hash"], finished=done, status="filled", filled=1,
            detail="filled: 2 attempt(s); re-priced once: 4.65–4.75 → 4.82–4.95, filled 4.88",
        )  # fmt: skip
        (r,) = recent_ideas(
            conn, now=done + dt.timedelta(hours=1), cfg=DedupeConfig.from_settings(settings)
        )
        assert r.kind is Suppression.EXECUTED and r.retry is None

    def test_partial_cancel_is_not_missed(self) -> None:
        settings = _settings()
        conn, first = _run(settings, _routines(), PipelineEnv.fixtures())
        (p,) = first.proposals
        done = FIXTURE_NOW + dt.timedelta(minutes=4)
        _miss(conn, p["proposal_hash"], finished=done, filled=1)
        (r,) = recent_ideas(
            conn, now=done + dt.timedelta(hours=1), cfg=DedupeConfig.from_settings(settings)
        )
        assert r.kind is Suppression.PROPOSED

    def test_no_retry_across_sessions(self) -> None:
        settings = _settings()
        conn, first = _run(settings, _routines(), PipelineEnv.fixtures())
        (p,) = first.proposals
        _miss(conn, p["proposal_hash"], finished=FIXTURE_NOW + dt.timedelta(minutes=4))
        monday = dt.datetime(2026, 9, 28, 10, 0, tzinfo=ET)  # next session: proposed cooldown
        (r,) = recent_ideas(conn, now=monday, cfg=DedupeConfig.from_settings(settings))
        assert r.kind is Suppression.PROPOSED and r.retry is None

    def test_restrictive_tier_scales_both_keys(self) -> None:
        s = _settings()
        base, strict = (
            DedupeConfig.from_settings(s, Tier.NORMAL),
            DedupeConfig.from_settings(s, Tier.RESTRICTIVE),
        )
        assert (base.missed_retry_after_minutes, base.missed_retry_max) == (30, 1)
        assert (strict.missed_retry_after_minutes, strict.missed_retry_max) == (60, 0)
        s3 = _settings(dedupe_missed_retry_max=3, dedupe_missed_retry_after_minutes=45)
        st3 = DedupeConfig.from_settings(s3, Tier.RESTRICTIVE)
        assert (st3.missed_retry_after_minutes, st3.missed_retry_max) == (90, 1)


def _routines():  # noqa: ANN202
    from arc.routines.config import load_routines

    cfg = load_routines()
    return cfg.model_copy(
        update={
            "director_diversification": cfg.director_diversification.model_copy(
                update={"mode": "strict"}
            )
        }
    )


# ---------------------------------------------------------------------------
# Rollback: missed_retry_max 0 = the pre-E24.2 verdicts, byte for byte
# ---------------------------------------------------------------------------


def _day(settings: ArcSettings) -> tuple[sqlite3.Connection, list[dict[str, Any]]]:
    """A fixture day: one idea, missed, then probed every 10 min for 3 hours."""
    conn, first = _run(settings, _routines(), PipelineEnv.fixtures())
    (p,) = first.proposals
    _miss(conn, p["proposal_hash"], finished=FIXTURE_NOW + dt.timedelta(minutes=4))
    cfg = DedupeConfig.from_settings(settings)
    out: list[dict[str, Any]] = []
    for k in range(18):
        now = FIXTURE_NOW + dt.timedelta(minutes=10 * (k + 1))
        priors = recent_ideas(conn, now=now, cfg=cfg)
        fp = IdeaFingerprint.parse(priors[0].fingerprint)
        for spot in (None, D("770"), D("800")):
            v = check_idea(fp, priors, spot=spot, regime="risk_on", cfg=cfg)
            out.append({"priors": [r.dump() for r in priors], "verdict": v.model_dump(mode="json")})
    return conn, out


def test_missed_retry_max_zero_is_byte_identical_to_a_non_miss() -> None:
    """At 0 retries a no-fill miss classifies exactly like a pre-E24.2 cancel (a budget
    stop is untouched by E24.2), so every verdict and recorded prior is the same."""
    off = _settings(dedupe_missed_retry_max=0)
    _, missed = _day(off)
    conn, _ = _run(off, _routines(), PipelineEnv.fixtures())
    (h,) = _open_hashes(conn)
    _miss(conn, h, finished=FIXTURE_NOW + dt.timedelta(minutes=4), detail=BUDGET_STOP)
    cfg = DedupeConfig.from_settings(off)
    control: list[dict[str, Any]] = []
    for k in range(18):
        now = FIXTURE_NOW + dt.timedelta(minutes=10 * (k + 1))
        priors = recent_ideas(conn, now=now, cfg=cfg)
        fp = IdeaFingerprint.parse(priors[0].fingerprint)
        for spot in (None, D("770"), D("800")):
            v = check_idea(fp, priors, spot=spot, regime="risk_on", cfg=cfg)
            control.append(
                {"priors": [r.dump() for r in priors], "verdict": v.model_dump(mode="json")}
            )
    strip = [  # proposal hashes differ per run only by fixture identity: compare shapes
        {**d, "priors": [{k: v for k, v in p.items() if k != "ref"} for p in d["priors"]]}
        for d in missed
    ]
    ctrl = [
        {**d, "priors": [{k: v for k, v in p.items() if k != "ref"} for p in d["priors"]]}
        for d in control
    ]
    for a, b in zip(strip, ctrl, strict=True):
        a["verdict"].pop("prior"), b["verdict"].pop("prior")
        a["verdict"]["detail"] = a["verdict"]["detail"].rsplit(" (", 1)[0]
        b["verdict"]["detail"] = b["verdict"]["detail"].rsplit(" (", 1)[0]
    assert strip == ctrl
    assert all(
        set(p) == set(RecentIdea.model_fields) - {"retry", "label"}
        for d in missed
        for p in d["priors"]
    )
    assert all(d["verdict"]["retry"] is None for d in missed)


# ---------------------------------------------------------------------------
# Research stage
# ---------------------------------------------------------------------------


def test_research_lines_hold_waiting_and_list_due_retries() -> None:
    from arc.pipeline.portfolio_context import PortfolioContext

    cands = {"TSLA": Stance.BEARISH}
    pctx = PortfolioContext.model_construct(positions=[])
    now = dt.datetime(2026, 10, 12, 13, 30, tzinfo=ET)
    held, lines = _held_and_recent(cands, [_missed_prior("wait")], pctx, now, DedupeConfig())
    assert held == {} and len(lines) == 1 and "one retry from 13:46 ET" in lines[0]
    assert _retry_lines(cands, [_missed_prior("wait")]) == []
    _, lines = _held_and_recent(cands, [_missed_prior("retry")], pctx, now, DedupeConfig())
    assert lines == []  # re-admitted: not "recently suggested"
    assert _retry_lines(cands, [_missed_prior("retry")]) == [
        "Retry: TSLA bearish put debit (missed 13:16 ET, 1/1)"
    ]
    assert _retry_lines({}, [_missed_prior("retry")]) == []  # not in today's pool


def test_research_lines_round_spot_to_cents() -> None:
    """A float-derived spot (195.29000000000002) prints as 195.29, not the raw repr."""
    from arc.pipeline.portfolio_context import PortfolioContext

    cands = {"TSLA": Stance.BEARISH}
    pctx = PortfolioContext.model_construct(positions=[])
    now = dt.datetime(2026, 10, 12, 13, 30, tzinfo=ET)
    prior = _missed_prior("wait").model_copy(update={"spot": D(str(195.29 + 1e-13))})
    _, lines = _held_and_recent(cands, [prior], pctx, now, DedupeConfig())
    assert "from 195.29 / regime changes" in lines[0]


def test_retry_lines_cap_one_per_idea_five_total() -> None:
    priors = []
    cands: dict[str, Stance] = {}
    for n in range(8):
        fp = IdeaFingerprint(
            ticker=f"T{n}", stance=Stance.BULLISH, structure_type="long_call",
            expiry_week="2026-W44", strike_bucket=100,
        )  # fmt: skip
        cands[fp.ticker] = Stance.BULLISH
        p = _missed_prior("retry").model_copy(
            update={"fingerprint": fp.key(), "label": "long call"}
        )
        priors += [p, p]
    lines = _retry_lines(cands, priors)
    assert len(lines) == MAX_RETRY_LINES == 5
    assert len(set(lines)) == 5


def test_prompt_section_only_when_recorded() -> None:
    from arc.personas.builders import ResearchInput, _missed_retry_section

    base = ResearchInput(
        candidates_json="", regime_features_json="", portfolio_summary="", scan_date=""
    )
    assert _missed_retry_section(base) == ""
    sec = _missed_retry_section(
        ResearchInput(
            candidates_json="", regime_features_json="", portfolio_summary="", scan_date="",
            missed_retry="Retry: TSLA bearish put debit (missed 13:16 ET, 1/1)",
        )
    )  # fmt: skip
    assert "Retry: TSLA bearish put debit (missed 13:16 ET, 1/1)" in sec


# ---------------------------------------------------------------------------
# Registry + labels
# ---------------------------------------------------------------------------


def test_registry_keys_and_bounds() -> None:
    after = REGISTRY["dedupe.missed_retry_after"]
    mx = REGISTRY["dedupe.missed_retry_max"]
    assert (after.field, after.min, after.max, after.unit) == (
        "dedupe_missed_retry_after_minutes",
        5,
        240,
        "m",
    )
    assert (mx.field, mx.min, mx.max) == ("dedupe_missed_retry_max", 0, 3)
    assert parse_value(mx, "0") == 0  # the documented rollback
    assert parse_value(after, "45") == 45
    for t, bad in ((after, "4"), (after, "241"), (mx, "4")):
        with pytest.raises(TunableError):
            parse_value(t, bad)
    s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
    assert (s.dedupe_missed_retry_after_minutes, s.dedupe_missed_retry_max) == (30, 1)


def test_reason_labels_are_plain() -> None:
    assert REASON_LABELS[ReasonCode.DEDUPE_MISSED_RETRY] == "Re-admitted: retry of a missed open"
    assert REASON_LABELS[ReasonCode.DEDUPE_MISSED_WAIT] == "Missed open: waiting before its retry"
    assert ReasonCode.DEDUPE_MISSED_RETRY.value == "dedupe:missed_retry"
    assert ReasonCode.DEDUPE_MISSED_WAIT.value == "dedupe:missed_wait"


def test_shortlist_missed_rows_are_noted_not_rejected(routines) -> None:  # noqa: ANN001
    settings = _settings()
    conn, first = _run(settings, routines, PipelineEnv.fixtures())
    (p,) = first.proposals
    done = FIXTURE_NOW + dt.timedelta(minutes=4)
    _miss(conn, p["proposal_hash"], finished=done)
    _run(settings, routines, PipelineEnv.fixtures(), conn=conn, now=done + dt.timedelta(minutes=5))
    row = conn.execute(
        "SELECT choice, payload FROM decisions WHERE reason_code='dedupe:missed_wait' "
        "AND stage='shortlist'"
    ).fetchone()
    assert row["choice"] == "noted"
    assert '"status": "wait"' in row["payload"] or '"status":"wait"' in row["payload"]
