"""E8.3 control tower: read-only data layer, Tailscale-only bind, Streamlit page, CLI."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from arc.cli import main
from arc.context.ttl import to_db
from arc.monitoring.store import HeartbeatRepo
from arc.store.db import connect
from arc.store.execution import ExecutionRepo, OpenStructureRepo
from arc.store.migrate import migrate
from arc.store.repos import (
    CandidateRepo,
    GateDecisionRepo,
    HaltRepo,
    PnlSnapshotRepo,
    PositionsSnapshotRepo,
    ProposalRepo,
)
from arc.structures import credit_vertical
from arc.tower import net
from arc.tower.cli import streamlit_command
from arc.tower.data import connect_ro, load_snapshot, parse_ts
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from arc.models import Structure

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 9, 28, 15, 0, tzinfo=ET)
SHORT, LONG = "SPY261030P00711000", "SPY261030P00710000"


def bull_put() -> Structure:
    return credit_vertical(
        "put", "SPY", dt.date(2026, 10, 30), short_strike=711, short_premium="5.60",
        long_strike=710, long_premium="4.70", as_of=dt.date(2026, 9, 25),
    )  # fmt: skip


def _proposal(
    conn: sqlite3.Connection,
    phash: str,
    ticker: str,
    *,
    passed: bool,
    violations: list[str] | None = None,
    at: dt.datetime = NOW,
    iso: bool = True,
) -> str:
    st = bull_put()
    cid = CandidateRepo(conn).insert(
        ticker=ticker, stance="bullish", catalyst_type="t", confidence=0.7
    )
    ProposalRepo(conn).insert(
        candidate_id=cid, proposal_hash=phash, structure_json=st.model_dump_json(),
        thesis="t", quant_json=json.dumps({"pop": 0.62, "ev": "12.5", "cost_bps": 0}),
        sizing_json=json.dumps({"contracts": 2, "notional": "140", "pct_equity": 0.01}),
        expires_at=to_db(at), created_at=at.isoformat() if iso else to_db(at),
        day=at.date().isoformat(), ticker=ticker,
    )  # fmt: skip
    GateDecisionRepo(conn).insert(
        proposal_hash=phash, passed=passed, violations=violations or [],
        decided_at=at.isoformat() if iso else to_db(at),
    )  # fmt: skip
    return cid


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A file DB with one of everything the tower shows."""
    path = tmp_path / "arc.db"
    conn = connect(path)
    migrate(conn)
    # proposals: one passed + approved + filled, one failed (two violations), one old
    cid = _proposal(conn, "a" * 64, "SPY", passed=True)
    bad = ["delta_cap: post-trade |Δ| 400 > cap 300", "stale_quote: SPY old"]
    _proposal(conn, "b" * 64, "QQQ", passed=False, iso=False, violations=bad)
    _proposal(conn, "c" * 64, "IWM", passed=False, violations=["halted: manual"],
              at=NOW - dt.timedelta(days=20))  # fmt: skip
    conn.execute(
        """INSERT INTO approval_requests
               (proposal_hash, ticker, day, proposal_json, status, channel, expires_at,
                created_at) VALUES (?, 'SPY', ?, '{}', 'approved', 'log', ?, ?)""",
        ("a" * 64, NOW.date().isoformat(), to_db(NOW), to_db(NOW)),
    )
    ex = ExecutionRepo(conn)
    ex.start(
        proposal_hash="a" * 64, kind="open", token_version="arc2", band_lo=D("-0.95"),
        band_hi=D("-0.85"), max_steps=3, contracts=2, now=NOW,
    )  # fmt: skip
    ex.finish("a" * 64, status="filled", filled_qty=2, fill_price=D("-0.90"), now=NOW)
    sid = OpenStructureRepo(conn).open(
        ticker="SPY", open_proposal_hash="a" * 64, candidate_id=cid,
        structure_json=bull_put().model_dump_json(), contracts=2, entry_net=D("-0.90"),
        now=NOW - dt.timedelta(hours=5),
    )  # fmt: skip
    PositionsSnapshotRepo(conn).insert(
        positions_json=json.dumps({"day": "2026-09-25", "structures": [
            {"structure_id": sid, "ticker": "SPY", "legs": {}, "held": True}]}),
        snapshot_at=to_db(NOW - dt.timedelta(days=3)),
    )  # fmt: skip
    for day, eq in (("2026-08-31", "100000"), ("2026-09-25", "100400")):
        PnlSnapshotRepo(conn).insert(
            realized="10", unrealized="-15", total="-5",
            details_json=json.dumps({"day": day, "equity": eq, "day_pnl": "40", "clean": True}),
            snapshot_at=f"{day}T20:30:00.000000Z",
        )  # fmt: skip
    HeartbeatRepo(conn).record(
        "monitor", "ok", at=NOW - dt.timedelta(minutes=10),
        detail={"valued": True, "positions": 1, "delta": 25.0, "gamma": -0.5, "vega": -300.0,
                "theta": 4.2, "max_loss": 140.0, "equity": 100500.0, "last_equity": 100400.0,
                "legs": [
                    {"symbol": SHORT, "qty": "-2", "side": "short", "unrealized_pl": "-20"},
                    {"symbol": LONG, "qty": "2", "side": "long", "unrealized_pl": "5"},
                ]},
    )  # fmt: skip
    HeartbeatRepo(conn).record("tick", "ok", at=NOW - dt.timedelta(minutes=3))
    HaltRepo(conn).halt(reason="reconciliation mismatch", actor="arc:reconcile",
                        at=to_db(NOW - dt.timedelta(hours=1)))  # fmt: skip
    old = HaltRepo(conn).halt(reason="manual", actor="U1", at=to_db(NOW - dt.timedelta(days=2)))
    HaltRepo(conn).resume(old, actor="U1", at=to_db(NOW - dt.timedelta(days=1)))
    conn.execute(
        "INSERT INTO ops_alerts (id, key, kind, message, opened_at) VALUES (?, ?, ?, ?, ?)",
        ("al-1", "tick_stale", "tick_stale", "no tick for 20m", to_db(NOW)),
    )
    conn.commit()
    conn.close()
    return path


def _snap(db: Path, **kw: object):
    conn = connect_ro(db)
    try:
        return load_snapshot(conn, now=NOW, db_path=str(db), **kw)  # type: ignore[arg-type]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# data layer
# ---------------------------------------------------------------------------


def test_snapshot_sections(db: Path) -> None:
    s = _snap(db, delta_cap=0.30, vega_cap_pct=0.005)
    # P&L: reconcile + intraday + MTD from the daily series
    assert s.pnl.reconciled_day == dt.date(2026, 9, 25)
    assert s.pnl.realized == D("10") and s.pnl.unrealized == D("-15")
    assert s.pnl.intraday_equity == D("100500") and s.pnl.intraday_day_pnl == D("100")
    assert s.pnl.performance is not None and s.pnl.performance.mtd_pnl == pytest.approx(400)
    assert s.pnl.reconcile_clean is True
    # Greeks with gate caps
    assert s.greeks.valued and s.greeks.delta == 25.0
    assert s.greeks.delta_cap == pytest.approx(301.5)  # 0.30 x 100500 / 100
    assert s.greeks.vega_usd == pytest.approx(-3.0)
    assert s.greeks.vega_cap_usd == pytest.approx(502.5)
    # positions: structure with its legs' P&L and the reconcile held flag
    assert len(s.structures) == 1
    st = s.structures[0]
    assert st.ticker == "SPY" and st.contracts == 2 and st.held is True
    assert st.unrealized_pl == D("-15") and st.expiration == dt.date(2026, 10, 30)
    assert [leg.symbol for leg in s.legs] == [SHORT, LONG]
    # proposals within 7 days, newest first, with gate / approval / execution
    assert {p.ticker for p in s.proposals} == {"SPY", "QQQ"}
    spy = next(p for p in s.proposals if p.ticker == "SPY")
    assert spy.gate_passed is True and spy.approval == "approved"
    assert spy.execution == "filled" and spy.fill_price == D("-0.90")
    assert spy.ev == D("12.5") and spy.contracts == 2
    # halts: active first
    assert s.halted and s.halts[0].active and s.halts[0].actor == "arc:reconcile"
    assert not s.halts[1].active and s.halts[1].cleared_by == "U1"
    # violations: split into rule code + detail; the 20-day-old one is outside the window
    assert {v.code for v in s.violations} == {"delta_cap", "stale_quote"}
    assert s.violation_counts == {"delta_cap": 1, "stale_quote": 1}
    # ops
    assert s.ops.tick_status == "ok" and s.ops.open_alerts[0]["kind"] == "tick_stale"


def test_lookback_widens_window(db: Path) -> None:
    s = _snap(db, lookback_days=30)
    assert {p.ticker for p in s.proposals} == {"SPY", "QQQ", "IWM"}
    assert s.violation_counts["halted"] == 1


def test_empty_db_renders_nothing_but_does_not_fail(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    conn = connect(path)
    migrate(conn)
    conn.close()
    s = _snap(path)
    assert s.structures == [] and s.proposals == [] and s.halts == [] and not s.halted
    assert s.greeks.at is None and s.pnl.reconciled_at is None and s.pnl.performance is None


def test_connect_ro_refuses_writes_and_missing_file(db: Path, tmp_path: Path) -> None:
    conn = connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO halts (id, at) VALUES ('x', 'y')")
    conn.close()
    missing = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        connect_ro(missing)
    assert not missing.exists()


def test_load_snapshot_issues_only_selects(db: Path) -> None:
    conn = connect_ro(db)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    load_snapshot(conn, now=NOW)
    conn.close()
    stmts = [s.strip().split()[0].upper() for s in seen if s.strip()]
    assert stmts and set(stmts) <= {"SELECT", "PRAGMA"}


def test_parse_ts_formats() -> None:
    assert parse_ts("2026-09-28T19:00:00.000000Z") == NOW
    assert parse_ts(NOW.isoformat()) == NOW
    assert parse_ts("2026-09-28 19:00:00") == NOW  # SQLite default: naive UTC
    assert parse_ts(None) is None and parse_ts("garbage") is None


def test_tower_imports_no_broker_llm_or_writers() -> None:
    """Read-only by construction: no broker, market data, LLM, Slack or execution import."""
    banned = (
        "arc.broker", "arc.data", "arc.execution", "arc.personas", "arc.slack.client",
        "arc.approvals", "anthropic", "openai", "alpaca", "slack_sdk", "requests",
    )  # fmt: skip
    for f in (REPO / "arc" / "tower").glob("*.py"):
        text = f.read_text()
        for b in banned:
            assert f"import {b}" not in text and f"from {b} " not in text, (f.name, b)
        assert "INSERT" not in text and "UPDATE " not in text and "DELETE" not in text, f.name


# ---------------------------------------------------------------------------
# bind address: Tailscale or loopback only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("addr", "ok"),
    [
        ("100.101.102.103", True),
        ("100.64.0.1", True),
        ("100.127.255.254", True),
        ("127.0.0.1", True),
        ("0.0.0.0", False),
        ("10.0.0.10", False),
        ("192.168.1.5", False),
        ("100.128.0.1", False),
        ("::", False),
        ("not-an-ip", False),
    ],
)
def test_allowed_bind(addr: str, ok: bool) -> None:
    assert net.allowed_bind(addr) is ok


def test_cgnat_addresses_from_ifconfig_and_cli() -> None:
    ifconfig = (
        "lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\n"
        "en0: flags=8863\n\tinet 10.0.0.10 netmask 0xffffff00 broadcast 10.0.0.255\n"
        "utun4: flags=8051\n\tinet 100.88.1.2 --> 100.88.1.2 netmask 0xffffffff\n"
    )
    assert net.cgnat_addresses(ifconfig) == ["100.88.1.2"]
    assert net.cgnat_addresses("100.70.0.9\n") == ["100.70.0.9"]
    assert net.cgnat_addresses("inet addr:100.90.0.1  Bcast") == ["100.90.0.1"]


def test_resolve_explicit_local_and_refusals() -> None:
    assert net.resolve_bind_address("100.99.0.1") == "100.99.0.1"
    assert net.resolve_bind_address(local=True) == "127.0.0.1"
    with pytest.raises(net.NoTailscaleAddressError, match="refusing to bind"):
        net.resolve_bind_address("0.0.0.0")


def test_resolve_prefers_tailscale_cli_then_interfaces() -> None:
    outputs = {"/x/tailscale": "100.77.0.5\n", "ifconfig": "inet 100.66.0.1 netmask"}

    def fake_run(argv: list[str]) -> str:
        return outputs.get(argv[0], "")

    with (
        mock.patch.object(net, "_tailscale_cli", return_value="/x/tailscale"),
        mock.patch.object(net, "_run", side_effect=fake_run),
    ):
        assert net.resolve_bind_address() == "100.77.0.5"
    with (
        mock.patch.object(net, "_tailscale_cli", return_value=None),
        mock.patch.object(net.shutil, "which", return_value="/sbin/ifconfig"),
        mock.patch.object(net, "_run", side_effect=fake_run),
    ):
        assert net.resolve_bind_address() == "100.66.0.1"


def test_resolve_fails_closed_without_tailscale() -> None:
    with (
        mock.patch.object(net, "_tailscale_cli", return_value=None),
        mock.patch.object(net.shutil, "which", return_value="/sbin/ifconfig"),
        mock.patch.object(net, "_run", return_value="inet 10.0.0.10 netmask\ninet 0.0.0.0"),
        pytest.raises(net.NoTailscaleAddressError, match="no Tailscale"),
    ):
        net.resolve_bind_address()


def test_run_swallows_missing_binary() -> None:
    assert net._run(["/definitely/not/a/binary"]) == ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_streamlit_command_is_hardened() -> None:
    argv = streamlit_command("100.77.0.5", 8501)
    pairs = dict(zip(argv[5::2], argv[6::2], strict=False))
    assert argv[1:4] == ["-m", "streamlit", "run"] and argv[4].endswith("arc/tower/app.py")
    assert pairs["--server.address"] == "100.77.0.5" and pairs["--server.port"] == "8501"
    assert pairs["--server.headless"] == "true"
    assert pairs["--server.enableXsrfProtection"] == "true"
    assert pairs["--browser.gatherUsageStats"] == "false"


def test_cli_serve_binds_tailscale_on_8501(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        mock.patch("arc.tower.net.resolve_bind_address", return_value="100.77.0.5"),
        mock.patch("arc.tower.cli.subprocess.call", return_value=0) as call,
    ):
        assert main(["tower", "serve", "--db", str(db)]) == 0
    argv = call.call_args.args[0]
    env = call.call_args.kwargs["env"]
    assert argv[argv.index("--server.address") + 1] == "100.77.0.5"
    assert argv[argv.index("--server.port") + 1] == "8501"
    assert env["ARC_TOWER_DB"] == str(db.resolve()) and env["ARC_TOWER_REFRESH"] == "30"
    assert "http://100.77.0.5:8501" in capsys.readouterr().out


def test_cli_serve_refuses_without_tailscale(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    err = net.NoTailscaleAddressError("no Tailscale IPv4 address found")
    with (
        mock.patch("arc.tower.net.resolve_bind_address", side_effect=err),
        mock.patch("arc.tower.cli.subprocess.call") as call,
    ):
        assert main(["tower", "serve", "--db", str(db)]) == 2
    call.assert_not_called()
    assert "no Tailscale" in capsys.readouterr().out
    with mock.patch("arc.tower.cli.subprocess.call") as call:
        assert main(["tower", "serve", "--db", str(db), "--address", "0.0.0.0"]) == 2
    call.assert_not_called()


def test_cli_serve_print_command_and_missing_db(
    db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tower", "serve", "--db", str(db), "--local", "--print-command"]) == 0
    assert "--server.address 127.0.0.1" in capsys.readouterr().out
    missing = tmp_path / "missing.db"
    assert main(["tower", "serve", "--db", str(missing), "--local"]) == 2
    assert not missing.exists()


def test_cli_snapshot(db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tower", "snapshot", "--db", str(db), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["greeks"]["delta"] == 25.0 and payload["violation_counts"]
    assert main(["tower", "snapshot", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "halted: YES (1 active)" in out and "1 open structure(s)" in out
    assert main(["tower", "snapshot", "--db", str(tmp_path / "x.db")]) == 2
    assert not (tmp_path / "x.db").exists()


def test_cli_does_not_modify_db(db: Path) -> None:
    before = db.read_bytes()
    main(["tower", "snapshot", "--db", str(db), "--json"])
    assert db.read_bytes() == before


# ---------------------------------------------------------------------------
# Streamlit page (AppTest: renders headless, no server)
# ---------------------------------------------------------------------------


def _app(db: Path, monkeypatch: pytest.MonkeyPatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("ARC_TOWER_DB", str(db))
    monkeypatch.setenv("ARC_TOWER_REFRESH", "0")
    at = AppTest.from_file(str(REPO / "arc" / "tower" / "app.py"), default_timeout=30)
    return at.run()


def test_app_renders_every_section(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    at = _app(db, monkeypatch)
    assert not at.exception
    subheaders = [s.value for s in at.subheader]
    assert subheaders == [
        "P&L",
        "Greeks (net portfolio)",
        "Positions",
        "Proposals",
        "Halts",
        "Gate violations",
    ]
    assert any("HALTED" in e.value for e in at.error)
    labels = {m.label for m in at.metric}
    assert {"Equity", "Day P&L", "Δ (share-eq)", "ν ($/vol-pt)", "Max loss"} <= labels
    assert len(at.dataframe) >= 4
    # read-only: no widgets that could change state
    assert not at.button and not at.text_input and not at.selectbox and not at.checkbox


def test_app_missing_db_shows_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    at = _app(tmp_path / "missing.db", monkeypatch)
    assert not at.exception
    assert any("audit store not found" in e.value for e in at.error)
    assert not (tmp_path / "missing.db").exists()


def test_app_empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "empty.db"
    conn = connect(path)
    migrate(conn)
    conn.close()
    at = _app(path, monkeypatch)
    assert not at.exception
    assert any("Trading enabled" in s.value for s in at.success)
