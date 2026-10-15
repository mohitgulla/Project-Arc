"""E15.6 (D69): Tower API arm-scoped reads (``?arm=``), defaulting to control.

Fixtures: the tests/test_tower.py control store (Production, $100.5k marks) plus
experiment arm stores recorded in it at t0 (``routine_state experiment_arm:<xp>:<arm>``):

* ``XP-2:treatment`` running, its own virtual-account marks ($25.3k) and a TSLA trade;
* ``XP-5:t1`` running (an A/A arm that runs its own Scout: owns the universe);
* ``XP-3:treatment`` stopped (404);
* ``XP-6:treatment`` running but its store file is gone (404).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arc.context.ttl import to_db
from arc.experiments.arms import ArmIdentity, write_identity
from arc.experiments.models import StopReason
from arc.monitoring.store import HeartbeatRepo
from arc.routines.runs import RoutineStateRepo
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo, PnlSnapshotRepo
from arc.tower.api import create_app
from arc.tower.scope import (
    ArmScope,
    UnknownArmError,
    arm_label,
    arm_short,
    list_arms,
    resolve_scope,
)
from tests import experiment_fixtures as fx
from tests.golden_compare import assert_json_close
from tests.test_tower import NOW, _proposal
from tests.test_tower import db as control_db  # noqa: F401 - fixture re-export
from tests.tower_routes_golden import YAML_ROUTES, dump, route_urls

GOLDEN = Path(__file__).parent / "fixtures" / "tower_routes_golden.json"
ARM = "XP-2:treatment"
OWNER = "XP-5:t1"
STOPPED = "XP-3:treatment"
MISSING = "XP-6:treatment"
ARM_HASH = "d" * 64

#: Routes that read the arm's own store with ?arm= (its virtual account, fills, runs).
SCOPED = {
    "/api/health", "/api/snapshot", "/api/overview", "/api/positions", "/api/trades",
    "/api/trades/filters", "/api/search", "/api/performance", "/api/performance/breakdown",
    "/api/ops/halts", "/api/ops/runs", "/api/ops/budget", "/api/ops/context",
    "/api/meta",
}  # fmt: skip
#: Routes fed by control-only data: control's response, flagged shared_from_control.
SHARED = {
    "/api/performance/funnel", "/api/ops/session", "/api/ops/health", "/api/ops/alerts",
    "/api/ops/sources", "/api/ops/llm", "/api/ops/config", "/api/ops/universe",
    "/api/experiments",
}  # fmt: skip


def _ident(control: Path, aid: str, *, personas: list[str] | None = None) -> ArmIdentity:
    xp, _, arm = aid.partition(":")
    return ArmIdentity(
        arm_id=aid,
        experiment_id=xp,
        arm=arm,
        spec_arm=arm,
        keys_env="ALPACA_EXP",
        control_db=str(control),
        created_at=NOW - dt.timedelta(days=3),
        plan={"fork_step": "quant.propose", "arm_personas": personas or [], "arm_jobs": []},
        account_mode="shared",
        arm_code="2",
    )


def _arm_store(path: Path, control: Path, aid: str, **kw: Any) -> Path:
    conn = connect(path)
    migrate(conn)
    write_identity(conn, _ident(control, aid, **kw))
    conn.close()
    return path


def _fill_arm(path: Path) -> None:
    """The arm's own book: virtual-account EOD rows, a monitor mark, a trade, a halt."""
    conn = connect(path)
    for day, eq in (("2026-09-24", "25000"), ("2026-09-25", "25300")):
        PnlSnapshotRepo(conn).insert(
            realized="0", unrealized="300", total="300",
            details_json=json.dumps({"day": day, "equity": eq, "virtual_equity": eq,
                                     "day_pnl": "300", "clean": True}),
            snapshot_at=f"{day}T20:30:00.000000Z",
        )  # fmt: skip
    HeartbeatRepo(conn).record(
        "monitor", "ok", at=NOW - dt.timedelta(minutes=5),
        detail={"valued": True, "positions": 0, "equity": 25321.5, "cash": 25321.5,
                "last_equity": 25300.0, "prev_close": "25300", "prev_close_source": "arc_close",
                "legs": []},
    )  # fmt: skip
    _proposal(conn, ARM_HASH, "TSLA", passed=True)
    HaltRepo(conn).halt(
        reason="book_invariant: 1 violation(s) (arm:qty)", actor="arc:experiments",
        at=to_db(NOW - dt.timedelta(minutes=30)),
    )  # fmt: skip
    conn.commit()
    conn.close()


@pytest.fixture
def stores(control_db: Path, tmp_path: Path) -> dict[str, Path]:  # noqa: F811
    arms = tmp_path / "arms"
    arms.mkdir()
    paths = {
        ARM: _arm_store(arms / "arc-exp-XP-2.db", control_db, ARM),
        OWNER: _arm_store(arms / "arc-exp-XP-5.db", control_db, OWNER, personas=["scout"]),
        STOPPED: _arm_store(arms / "arc-exp-XP-3.db", control_db, STOPPED),
        MISSING: arms / "arc-exp-XP-6.db",  # never created
    }
    _fill_arm(paths[ARM])
    conn = connect(control_db)
    conn.row_factory = sqlite3.Row
    fx.start(conn, fx.spec("XP-2"))
    fx.start(conn, fx.spec("XP-5", kind="aa"))
    s3 = fx.start(conn, fx.spec("XP-3", area="sizing"))
    s3.stop("XP-3", StopReason.OWNER, actor="local")
    fx.start(conn, fx.spec("XP-6", kind="aa"))
    state = RoutineStateRepo(conn)
    for aid, p in paths.items():
        state.set(f"experiment_arm:{aid}", str(p), now=NOW)
    conn.close()
    paths["control"] = control_db
    return paths


@pytest.fixture
def static(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html>")
    return d


@pytest.fixture
def client(stores: dict[str, Path], static: Path) -> TestClient:
    return TestClient(create_app(stores["control"], static_dir=static, clock=lambda: NOW))


def _get(client: TestClient, url: str, arm: str | None = None) -> Any:
    params = {"arm": arm} if arm is not None else None
    r = client.get(url, params=params)
    return r


def _strip(body: Any) -> Any:
    if isinstance(body, dict):
        return {k: v for k, v in body.items() if k not in ("scope", "shared_from_control", "db")}
    return body


# ---------------------------------------------------------------------------
# no ?arm: exactly main's responses
# ---------------------------------------------------------------------------


def test_no_param_responses_unchanged_from_main(control_db: Path, static: Path) -> None:  # noqa: F811
    """Every route without ?arm (minus the two new keys) equals the golden captured on main
    before E15.6 (``python -m tests.tower_routes_golden``, main tree on PYTHONPATH)."""
    client = TestClient(create_app(control_db, static_dir=static, clock=lambda: NOW))
    got = json.loads(json.dumps(dump(client, "a" * 64, golden_only=True), sort_keys=True))
    assert_json_close(got, json.loads(GOLDEN.read_text()))


def test_every_route_without_arm_is_control_scope(client: TestClient) -> None:
    for url in route_urls(client, "a" * 64):
        body = client.get(url).json()
        if url.endswith("/nope") or url.endswith("/XP-1"):
            assert body["error"] == "not_found"
            continue
        assert body["scope"] == {"arm_id": None}, url
        assert body["shared_from_control"] is False, url


def test_arm_control_is_the_default(client: TestClient) -> None:
    """``?arm=control`` and ``?arm=`` serve the very same bodies as no parameter."""
    plain = dump(client, "a" * 64, strip_e156=False)
    for arm in ("control", ""):
        for url, want in plain.items():
            got = client.get(url, params={"arm": arm})
            assert got.status_code == want["status"], (arm, url)
            assert got.json() == client.get(url).json(), (arm, url)


# ---------------------------------------------------------------------------
# ?arm=<id>: every route
# ---------------------------------------------------------------------------


def test_every_route_serves_the_arm_scope(client: TestClient) -> None:
    urls = route_urls(client, ARM_HASH)
    seen = set()
    for url in urls:
        r = client.get(url, params={"arm": ARM})
        base = url.split("/" + ARM_HASH)[0]
        if url.endswith("/nope") or url.endswith("/XP-1"):
            assert r.status_code == 404 and r.json()["error"] == "not_found", url
            continue
        assert r.status_code == 200, (url, r.text[:300])
        body = r.json()
        assert body["scope"] == {"arm_id": ARM}, url
        key = "/api/trades/{hash}" if base != url else url
        if key in SHARED:
            assert body["shared_from_control"] is True, url
        else:
            assert key in SCOPED or key == "/api/trades/{hash}", url
            assert body["shared_from_control"] is False, url
        seen.add(key)
    assert seen | {"/api/trades/{hash}"} >= SCOPED | SHARED


def test_arm_routes_read_the_arm_store(client: TestClient, stores: dict[str, Path]) -> None:
    """Equity / P&L / trades / halts come from the arm's own rows, never control's."""
    snap = _get(client, "/api/snapshot", ARM).json()
    assert snap["db_path"] == str(stores[ARM].resolve())
    assert D(snap["pnl"]["equity"]) == D("25300")  # the arm's virtual-account EOD
    assert [p["ticker"] for p in snap["proposals"]] == ["TSLA"]
    ov = _get(client, "/api/overview", ARM).json()
    assert D(ov["equity"]["value"]) == D("25321.5")  # the arm's monitor mark
    assert D(_get(client, "/api/overview").json()["equity"]["value"]) == D("100500")
    assert ov["positions"] == []
    # D113: the arm's status strip shows production's tick (arms write no tick heartbeat)
    ctl_status = _get(client, "/api/overview").json()["status"]
    assert ctl_status["tick_at"] is not None
    assert ov["status"]["tick_at"] == ctl_status["tick_at"]
    assert ov["status"]["tick_status"] == ctl_status["tick_status"]
    assert _get(client, "/api/positions", ARM).json()["items"] == []
    trades = _get(client, "/api/trades", ARM).json()
    assert [t["ticker"] for t in trades["items"]] == ["TSLA"]
    assert _get(client, f"/api/trades/{ARM_HASH}", ARM).status_code == 200
    assert _get(client, f"/api/trades/{ARM_HASH}").status_code == 404  # not control's
    assert _get(client, f"/api/trades/{'a' * 64}", ARM).status_code == 404  # not the arm's
    halts = _get(client, "/api/ops/halts", ARM).json()["halts"]
    assert [h["reason"] for h in halts] == ["book_invariant: 1 violation(s) (arm:qty)"]
    assert _get(client, "/api/health", ARM).json()["db"] == str(stores[ARM].resolve())
    perf = _get(
        client,
        "/api/performance",
        ARM,
    ).json()
    ctl = _get(client, "/api/performance").json()
    assert perf != ctl  # the 60 s response cache keys on the store too


def test_shared_routes_serve_control_data(client: TestClient) -> None:
    for url in sorted(SHARED - {"/api/ops/universe"}):
        arm = _get(client, url, ARM).json()
        assert _strip(arm) == _strip(_get(client, url).json()), url


def test_universe_is_the_arms_when_it_runs_its_own_scout(client: TestClient) -> None:
    shared = _get(client, "/api/ops/universe", ARM).json()
    assert shared["shared_from_control"] is True
    assert _strip(shared) == _strip(_get(client, "/api/ops/universe").json())
    own = _get(client, "/api/ops/universe", OWNER).json()
    assert own["shared_from_control"] is False and own["scope"] == {"arm_id": OWNER}


@pytest.mark.parametrize("arm", ["XP-99:t1", STOPPED, MISSING, "control:x", "treatment"])
def test_unknown_stopped_or_missing_arm_is_404_with_valid_arms(
    client: TestClient, arm: str
) -> None:
    for url in ("/api/overview", "/api/meta", "/api/ops/sources", "/api/experiments"):
        r = _get(client, url, arm)
        assert r.status_code == 404, (arm, url)
        body = r.json()
        assert body["error"] == "unknown_arm"
        assert body["detail"].endswith(f"valid arms: control, {ARM}, {OWNER}")


def test_arm_query_is_bounded(client: TestClient) -> None:
    assert _get(client, "/api/overview", "x" * 65).status_code == 422


def test_still_read_only_with_arm(client: TestClient, stores: dict[str, Path]) -> None:
    before = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in stores.items()
              if p.is_file()}  # fmt: skip
    for url in route_urls(client, ARM_HASH):
        client.get(url, params={"arm": ARM})
        client.get(url, params={"arm": OWNER})
    assert client.post("/api/overview", params={"arm": ARM}).status_code == 405
    after = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in stores.items()
             if p.is_file()}  # fmt: skip
    assert before == after


# ---------------------------------------------------------------------------
# /api/meta arms
# ---------------------------------------------------------------------------


def test_meta_lists_arms_running_first(client: TestClient) -> None:
    arms = _get(client, "/api/meta").json()["arms"]
    assert [a["id"] for a in arms] == [ARM, OWNER, MISSING, STOPPED]
    by = {a["id"]: a for a in arms}
    assert by[ARM]["label"] == "XP-2 · T1" and by[ARM]["experiment_id"] == "XP-2"
    assert by[OWNER]["label"] == "XP-5 · T1"
    assert by[ARM]["status"] == "running" and by[ARM]["viewable"] is True
    assert isinstance(by[ARM]["store_age_s"], int) and by[ARM]["store_age_s"] >= 0
    assert by[STOPPED]["status"] == "stopped" and by[STOPPED]["viewable"] is False
    assert by[MISSING]["store_age_s"] is None and by[MISSING]["viewable"] is False
    # the arm list is control's, whatever ?arm= says
    assert _get(client, "/api/meta", ARM).json()["arms"] == arms


def test_meta_without_experiments_has_no_arms(control_db: Path, static: Path) -> None:  # noqa: F811
    client = TestClient(create_app(control_db, static_dir=static, clock=lambda: NOW))
    assert client.get("/api/meta").json()["arms"] == []


# ---------------------------------------------------------------------------
# scope helpers
# ---------------------------------------------------------------------------


def test_arm_labels() -> None:
    assert arm_short("treatment") == "T1"
    assert arm_short("t3") == "T3"
    assert arm_short("shadow_control") == "Shadow Control"
    assert arm_label("XP-12", "t1") == "XP-12 · T1"


def test_resolve_scope_control_needs_no_lookup(tmp_path: Path) -> None:
    ctl = tmp_path / "arc.db"
    assert resolve_scope(ctl, None, None) == ArmScope(db_path=ctl)
    assert resolve_scope(ctl, None, "control") == ArmScope(db_path=ctl)
    with pytest.raises(UnknownArmError) as e:
        resolve_scope(ctl, None, "XP-1:t1")
    assert e.value.valid == ["control"]


def test_resolve_scope_reads_arm_personas(stores: dict[str, Path]) -> None:
    conn = sqlite3.connect(f"file:{stores['control']}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        own = resolve_scope(stores["control"], conn, OWNER)
        plain = resolve_scope(stores["control"], conn, ARM)
    finally:
        conn.close()
    assert own.owns("scout", "trending") and not own.owns("scalp")
    assert plain.arm_personas == frozenset() and plain.db_path == stores[ARM].resolve()


def test_legacy_arm_key_resolves_through_the_store(tmp_path: Path) -> None:
    """A pre-D69 key (``experiment_arm:<arm>``) names its experiment in the arm store."""
    ctl = tmp_path / "arc.db"
    conn = connect(ctl)
    migrate(conn)
    conn.row_factory = sqlite3.Row
    fx.start(conn, fx.spec("XP-10"))
    arm = _arm_store(tmp_path / "arc-exp-XP-10.db", ctl, "XP-10:treatment")
    RoutineStateRepo(conn).set("experiment_arm:treatment", str(arm), now=NOW)
    RoutineStateRepo(conn).set("experiment_arm:ghost", str(tmp_path / "gone.db"), now=NOW)
    arms = list_arms(conn, now=NOW)
    assert [(a.id, a.label, a.status) for a in arms] == [
        ("XP-10:treatment", "XP-10 · T1", "running")
    ]
    assert resolve_scope(ctl, conn, "XP-10:treatment").db_path == arm.resolve()
    conn.close()


def test_list_arms_on_a_store_without_experiment_tables(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    assert list_arms(conn, now=NOW) == []
    with pytest.raises(UnknownArmError):
        resolve_scope(tmp_path / "x.db", conn, "XP-1:t1")
    conn.close()


def test_new_routes_and_yaml_routes_are_covered(client: TestClient) -> None:
    """A route added later must be classified (scoped or shared) here."""
    bases = {u for u in route_urls(client, ARM_HASH) if not u.endswith(("/nope", "/XP-1"))}
    bases = {"/api/trades/{hash}" if u.endswith(ARM_HASH) else u for u in bases}
    assert bases == SCOPED | SHARED | {"/api/trades/{hash}"}
    assert set(YAML_ROUTES) <= SCOPED | SHARED
