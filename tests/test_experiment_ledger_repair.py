"""E10.2c: the virtual ledger books each broker leg fill once (stable key, increments)
and ``arc experiment repair-ledger`` nets out fills booked twice before the fix.

XP-10 evidence: Alpaca returned the same mleg leg fill with ``filled_at`` 1 µs apart
on two calls, and the old ``order:symbol:filled_at:qty:price`` ref booked it twice.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as hst

from arc.broker.base import Fill
from arc.experiments.repair import duplicate_adjustments, repair_ledger
from arc.experiments.virtual import (
    LedgerRow,
    append_row,
    fill_ref,
    open_account,
    order_symbol,
    record_fills,
    replay,
    rows,
)
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

AID = "XP-10:treatment"
T0 = dt.datetime(2026, 10, 7, 0, 23, tzinfo=ET)
OCT8 = dt.datetime(2026, 10, 8, 10, 45, 20, 930773, tzinfo=ET)
OID = "b3a5f601-316f-42c8-aa05-8aad80c94342"
LONG, SHORT = "TSM261120C00470000", "TSM261120C00510000"


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def _leg(sym: str, side: str, qty: int, px: str, at: dt.datetime, oid: str = OID) -> Fill:
    return Fill(broker_order_id=oid, symbol=sym, side=side, qty=D(qty), price=D(px), filled_at=at)


def _mleg(at: dt.datetime) -> list[Fill]:
    return [
        _leg(LONG, "buy", 1, "21.5", at),
        _leg(SHORT, "sell_short", 1, "7.6", at + dt.timedelta(microseconds=4)),
    ]


def _fills(c: sqlite3.Connection) -> list[LedgerRow]:
    return [r for r in rows(c, AID) if r.kind in ("fill", "adjust")]


# --- the fix: one booking per fill --------------------------------------------------


def test_same_mleg_fill_fetched_twice_with_filled_at_drift_books_once() -> None:
    c = _db()
    open_account(c, AID, t0_equity=D("98354.85"), legacy={}, at=T0)
    assert record_fills(c, AID, _mleg(OCT8)) == 2
    # the next sync: same fill, Alpaca's leg filled_at is 1 µs later
    assert record_fills(c, AID, _mleg(OCT8 + dt.timedelta(microseconds=1))) == 0
    booked = _fills(c)
    assert len(booked) == 2
    assert sum(r.amount for r in booked) == D(-2150) + D(760)
    st = replay(rows(c, AID), as_of=OCT8.date())
    assert st.held == {LONG: D(1), SHORT: D(-1)}
    assert st.cash == D("98354.85") - 1390
    assert {r.ref for r in booked} == {f"{OID}:{LONG}:1", f"{OID}:{SHORT}:1"}


def test_partial_then_full_fill_books_the_increment_only() -> None:
    c = _db()
    open_account(c, AID, t0_equity=D(10000), legacy={}, at=T0)
    fee = D("0.65")
    at = OCT8
    # 2 of 5 at 3.00, then all 5 at an average 3.10 (Alpaca reports cumulative qty + avg px)
    assert record_fills(c, AID, [_leg(LONG, "buy", 2, "3.00", at)], fee_per_contract=fee) == 1
    assert record_fills(c, AID, [_leg(LONG, "buy", 2, "3.00", at)], fee_per_contract=fee) == 0
    full = _leg(LONG, "buy", 5, "3.10", at + dt.timedelta(minutes=3))
    assert record_fills(c, AID, [full], fee_per_contract=fee) == 1
    # a stale re-fetch of the partial after the full fill books nothing
    assert record_fills(c, AID, [_leg(LONG, "buy", 2, "3.00", at)], fee_per_contract=fee) == 0
    assert record_fills(c, AID, [full], fee_per_contract=fee) == 0
    booked = _fills(c)
    assert [r.detail["qty"] for r in booked] == ["2", "3"]
    total = -(D("3.10") * 5 * 100) - fee * 5
    assert sum(r.amount for r in booked) == total == D("-1553.25")
    assert replay(rows(c, AID), as_of=at.date()).held == {LONG: D(5)}


def test_sell_partial_then_full_settles_the_proceeds_next_session() -> None:
    c = _db()
    open_account(c, AID, t0_equity=D(10000), legacy={}, at=T0)
    record_fills(c, AID, [_leg(SHORT, "sell", 1, "2.00", OCT8)])
    record_fills(c, AID, [_leg(SHORT, "sell", 3, "2.20", OCT8 + dt.timedelta(minutes=1))])
    booked = _fills(c)
    assert sum(r.amount for r in booked) == D(660)
    assert all(r.settles_on == dt.date(2026, 10, 9) for r in booked)
    st = replay(rows(c, AID), as_of=OCT8.date())
    assert st.unsettled == 660 and st.held == {SHORT: D(-3)}


def test_new_key_continues_a_ledger_holding_pre_fix_refs() -> None:
    """An arm store booked before E10.2c (old ref format) is not booked again."""
    c = _db()
    open_account(c, AID, t0_equity=D(10000), legacy={}, at=T0)
    old = LedgerRow(
        arm_id=AID,
        kind="fill",
        ref=f"{OID}:{LONG}:{OCT8.isoformat()}:1:21.5",
        amount=D(-2150),
        settles_on=OCT8.date(),
        at=OCT8,
        detail={"symbol": LONG, "qty": "1", "price": "21.5"},
    )
    with c:
        append_row(c, old)
    assert order_symbol(old.ref) == (OID, LONG)
    assert record_fills(c, AID, [_leg(LONG, "buy", 1, "21.5", OCT8)]) == 0
    assert record_fills(c, AID, [_leg(LONG, "buy", 2, "21.5", OCT8)]) == 1
    assert sum(r.amount for r in _fills(c)) == D(-4300)


def test_fill_ref_is_stable_across_decimal_spellings() -> None:
    assert fill_ref("o", "S", D("2")) == fill_ref("o", "S", D("2.0")) == fill_ref("o", "S", D(-2))
    assert fill_ref("o", "S", D("2")) == "o:S:2"


@settings(max_examples=60, deadline=None)
@given(
    steps=hst.lists(hst.integers(min_value=1, max_value=10), min_size=1, max_size=8),
    drift=hst.lists(hst.integers(min_value=0, max_value=999), min_size=8, max_size=8),
    repeats=hst.integers(min_value=1, max_value=3),
)
def test_any_fetch_sequence_books_the_final_cumulative_fill(
    steps: list[int], drift: list[int], repeats: int
) -> None:
    """Re-fetches (with µs drift), stale partials and growing cumulative fills always
    total the broker's final cumulative fill, contracts and cash."""
    c = _db()
    open_account(c, AID, t0_equity=D(10000), legacy={}, at=T0)
    cum, last = 0, None
    for i, s in enumerate(steps):
        cum = max(cum, s if i % 3 == 2 else cum + s)  # some fetches are stale (lower)
        px = D("1.00") + D(cum) / 100
        for k in range(repeats):
            at = OCT8 + dt.timedelta(microseconds=drift[(i + k) % 8])
            f = _leg(LONG, "buy", min(s, cum) if i % 3 == 2 else cum, str(px), at)
            record_fills(c, AID, [f])
            if f.qty == cum:
                last = f
    assert last is not None
    st = replay(rows(c, AID), as_of=OCT8.date())
    assert st.held == {LONG: D(cum)}
    assert st.cash == D(10000) - last.price * cum * 100


# --- the repair ---------------------------------------------------------------------


def _xp10_arm(path: Path | str = ":memory:") -> sqlite3.Connection:
    """The XP-10 shape: a duplicate booked before an EOD (TSM) and one after (HOOD)."""
    c = _db(path)
    open_account(c, AID, t0_equity=D("98354.85"), legacy={}, at=T0)
    hood_at = dt.datetime(2026, 10, 8, 13, 14, 5, 855024, tzinfo=ET)
    hood = "fa3e4f1e-caaf-4566-b6a7-ed0624a694f4"

    def old(sym: str, side: str, qty: int, px: str, at: dt.datetime, oid: str) -> LedgerRow:
        sign = D(1) if side == "sell" else D(-1)
        return LedgerRow(
            arm_id=AID,
            kind="fill",
            ref=f"{oid}:{sym}:{at.astimezone(dt.UTC).isoformat()}:{qty}:{px}",
            amount=sign * D(px) * qty * 100,
            settles_on=dt.date(2026, 10, 9) if side == "sell" else at.date(),
            at=at,
            detail={"symbol": sym, "qty": str(-qty if side == "sell" else qty), "price": px},
        )

    us = dt.timedelta(microseconds=1)
    booked = [
        old(LONG, "buy", 1, "21.5", OCT8, OID),
        old(SHORT, "sell", 1, "7.6", OCT8 + 4 * us, OID),
        old(LONG, "buy", 1, "21.5", OCT8 + us, OID),  # duplicate, same day
        old(SHORT, "sell", 1, "7.6", OCT8 + 5 * us, OID),  # duplicate, same day
        old("HOOD261120P00110000", "buy", 2, "10.25", hood_at, hood),
        old("HOOD261120P00095000", "sell", 2, "3.5", hood_at + us, hood),
    ]
    with c:
        for r in booked:
            append_row(c, r)

    def eod(day: str, broker: list[tuple[str, str, str, str]], details: dict) -> None:
        at = f"{day}T20:30:08.000000Z"
        snap = {
            "day": day,
            "broker": [
                {"symbol": s, "qty": q, "side": side, "asset_class": "us_option",
                 "market_value": mv}
                for s, q, side, mv in broker
            ],
        }  # fmt: skip
        c.execute(
            "INSERT INTO positions_snapshots (id, snapshot_at, positions_json) VALUES (?, ?, ?)",
            (f"ps-{day}", at, json.dumps(snap)),
        )
        c.execute(
            """INSERT INTO pnl_snapshots (id, snapshot_at, realized, unrealized, total,
                   details_json) VALUES (?, ?, '0', '0', '0', ?)""",
            (f"pnl-{day}", at, json.dumps({"day": day, **details})),
        )
        c.commit()

    oct8 = [("HOOD261120P00095000", "-2", "short", "-760"),
            ("HOOD261120P00110000", "2", "long", "2070"),
            (LONG, "1", "long", "1635"), (SHORT, "-1", "short", "-620")]  # fmt: skip
    # stored by the buggy ledger: TSM double-booked (-1,390), HOOD's duplicate not yet
    eod("2026-10-08", oct8, {"equity": "91709.85", "virtual_equity": "91709.85",
        "cash": "80000", "last_equity": "98354.85", "prev_close": "98354.85",
        "prev_close_source": "broker_last_equity", "day_pnl": "-6645.00"})  # fmt: skip
    # the HOOD sell is booked a second time after Oct 8's EOD
    with c:
        append_row(c, old("HOOD261120P00095000", "sell", 2, "3.5", hood_at + 2 * us, hood))
    oct9 = [("HOOD261120P00095000", "-2", "short", "-610"),
            ("HOOD261120P00110000", "2", "long", "1810"),
            (LONG, "1", "long", "1345"), (SHORT, "-1", "short", "-425")]  # fmt: skip
    eod("2026-10-09", oct9, {"equity": "91709.85", "virtual_equity": "91709.85",
        "cash": "80000", "last_equity": "91709.85", "prev_close": "91709.85",
        "prev_close_source": "arc_close", "day_pnl": "0.00"})  # fmt: skip
    return c


def _snap(c: sqlite3.Connection, day: str) -> dict:
    raw = c.execute("SELECT details_json FROM pnl_snapshots WHERE id = ?", (f"pnl-{day}",))
    return json.loads(raw.fetchone()[0])


def test_duplicate_adjustments_net_each_leg_to_its_broker_fill() -> None:
    c = _xp10_arm()
    ids = [r[0] for r in c.execute("SELECT id FROM virtual_ledger ORDER BY id")]
    adj = duplicate_adjustments(list(zip(ids, rows(c, AID), strict=True)))
    assert [(a.symbol, a.qty, a.amount) for a in adj] == [
        (LONG, D(-1), D(2150)),
        (SHORT, D(1), D(-760)),
        ("HOOD261120P00095000", D(2), D(-700)),
    ]
    hood = adj[2]
    assert hood.settles_on == dt.date(2026, 10, 9)
    assert hood.at.astimezone(ET).date() == dt.date(2026, 10, 8)


def test_repair_appends_adjust_rows_restates_snapshots_and_is_idempotent() -> None:
    c = _xp10_arm()
    n_before = c.execute("SELECT count(*) FROM virtual_ledger").fetchone()[0]

    dry = repair_ledger(c, AID, dry_run=True)
    assert dry.fill_sum_before == D(-3430) and dry.fill_sum_after == D(-2740)
    assert c.execute("SELECT count(*) FROM virtual_ledger").fetchone()[0] == n_before
    assert _snap(c, "2026-10-08")["virtual_equity"] == "91709.85"  # dry run wrote nothing

    rep = repair_ledger(c, AID)
    assert [s.day for s in rep.snapshots] == ["2026-10-08", "2026-10-09"]
    # append-only: every original row is still there, plus three adjust rows
    assert c.execute("SELECT count(*) FROM virtual_ledger").fetchone()[0] == n_before + 3
    assert sum(r.amount for r in _fills(c)) == D(-2740)
    st = replay(rows(c, AID), as_of=dt.date(2026, 10, 9))
    assert st.held == {LONG: 1, SHORT: -1, "HOOD261120P00110000": 2, "HOOD261120P00095000": -2}
    # Oct 8 EOD: true cash 98354.85 - 2740 + marks 2325
    s8 = _snap(c, "2026-10-08")
    assert D(s8["virtual_equity"]) == D("97939.85")
    assert D(s8["equity"]) == D("97939.85") and D(s8["cash"]) == D("86230.00")
    assert D(s8["prev_close"]) == D("98354.85") and D(s8["day_pnl"]) == D("-415.00")
    assert s8["restated"]["virtual_equity"] == "91709.85"
    # Oct 9: prev close follows the restated Oct 8 close
    s9 = _snap(c, "2026-10-09")
    assert D(s9["virtual_equity"]) == D("97734.85")
    assert D(s9["prev_close"]) == D("97939.85")
    assert D(s9["day_pnl"]) == D("-205.00")
    # every Oct 8 duplicate was booked by Oct 9's start of day: +2150 - 760 - 700
    assert D(s9["last_equity"]) == D("92399.85")

    again = repair_ledger(c, AID)
    assert again.adjustments == [] and again.snapshots == []
    assert again.fill_sum_before == again.fill_sum_after == D(-2740)


def test_adjust_nets_out_the_duplicate_unsettled_proceeds() -> None:
    c = _xp10_arm()
    before = replay(rows(c, AID), as_of=dt.date(2026, 10, 8))
    assert before.unsettled == 760 * 2 + 700 * 2
    repair_ledger(c, AID)
    after = replay(rows(c, AID), as_of=dt.date(2026, 10, 8))
    assert after.unsettled == 760 + 700
    assert replay(rows(c, AID), as_of=dt.date(2026, 10, 9)).unsettled == 0


def test_virtual_ledger_stays_append_only_after_migration_034(tmp_path: Path) -> None:
    c = _xp10_arm(tmp_path / "arm.db")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM virtual_ledger")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE virtual_ledger SET amount = '0'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO virtual_ledger (arm_id, kind, ref, amount, at) "
            "VALUES ('a', 'bogus', 'r', '0', 'x')"
        )


def test_migration_034_keeps_every_ledger_row_and_id(tmp_path: Path) -> None:
    from arc.store.migrate import MIGRATIONS_DIR

    path = tmp_path / "old.db"
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        v = int(f.stem.split("_", 1)[0])
        if v >= 34:
            break
        c.executescript(f.read_text())
        c.execute("INSERT INTO schema_version (version) VALUES (?)", (v,))
    c.execute(
        "INSERT INTO virtual_ledger (id, arm_id, kind, ref, amount, at) "
        "VALUES (117, 'a', 'fill', 'o:S:t:1:2', '-200', 't')"
    )
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):  # 'adjust' is refused before 034
        c.execute(
            "INSERT INTO virtual_ledger (arm_id, kind, ref, amount, at) "
            "VALUES ('a', 'adjust', 'r', '0', 't')"
        )
    c.rollback()
    migrate(c)
    assert c.execute("SELECT id, ref, amount FROM virtual_ledger").fetchall() == [
        (117, "o:S:t:1:2", "-200")
    ]
    c.execute(
        "INSERT INTO virtual_ledger (arm_id, kind, ref, amount, at) "
        "VALUES ('a', 'adjust', 'r', '0', 't')"
    )
    with pytest.raises(sqlite3.IntegrityError):  # the unique ref index is back
        c.execute(
            "INSERT INTO virtual_ledger (arm_id, kind, ref, amount, at) "
            "VALUES ('a', 'adjust', 'r', '1', 't')"
        )


# --- the CLI ------------------------------------------------------------------------


def _arc(*argv: str) -> int:
    from arc.cli import main

    return main(list(argv))


def test_cli_repair_ledger_on_a_running_experiment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.experiments.config import ArmRunner, RunnerConfig
    from arc.experiments.evaluate import latest_report
    from arc.experiments.runner import arm_stores, start_arms

    ctl = tmp_path / "control.db"
    spec = "config/experiments/live/xp1_aa_baseline.yaml"
    assert (
        _arc("experiment", "create", "--owner-approval", "P-1", "--spec", spec, "--db", str(ctl))
        == 0
    )
    assert _arc("experiment", "register", "--owner-approval", "P-1", "XP-1", "--db", str(ctl)) == 0
    conn = _db(ctl)
    start_arms(
        conn,
        "XP-1",
        actor="local",
        now=T0,
        t0_equity=D("98354.85"),
        runner=RunnerConfig(
            arms={
                "treatment": ArmRunner(
                    spec_arm="treatment", keys_env="ALPACA_EXP", db="exp-{experiment_id}.db"
                )
            }  # fmt: skip
        ),
        arm_dir=tmp_path / "arms",
        control_sha="abcdef1",
    )
    arm_path = arm_stores(conn, "XP-1")["treatment"]
    conn.close()
    # an arm store booked by the pre-fix ledger (rows copied from the XP-10 shape)
    src = _xp10_arm()
    arm = _db(arm_path)
    aid = arm.execute("SELECT arm_id FROM arm_identity").fetchone()[0]
    with arm:
        for r in rows(src, AID):
            if r.kind != "open":
                append_row(arm, r.model_copy(update={"arm_id": aid}))
        for t in ("positions_snapshots", "pnl_snapshots"):
            cols = [x[1] for x in src.execute(f"PRAGMA table_info({t})")]
            for row in src.execute(f"SELECT {', '.join(cols)} FROM {t}"):  # noqa: S608
                arm.execute(
                    f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",  # noqa: S608
                    tuple(row),
                )
    arm.close()
    capsys.readouterr()

    db = ("--db", str(ctl))
    assert _arc("experiment", "repair-ledger", "XP-1", *db, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry run: XP-1:treatment: ledger fill sum -3,430.00 -> -2,740.00" in out
    assert latest_report(_db(ctl), "XP-1") is None  # a dry run stores nothing

    now = ("--now", "2026-10-09T17:00")
    assert _arc("experiment", "repair-ledger", "XP-1", *db, *now) == 0
    out = capsys.readouterr().out
    assert "appended 3 adjust row(s), 2 pnl snapshot(s) restated" in out
    assert "stored report #" in out
    assert latest_report(_db(ctl), "XP-1") is not None  # the day series re-stored
    restated = _db(arm_path)
    assert _snap(restated, "2026-10-08")["virtual_equity"] == "97939.85"

    assert _arc("experiment", "repair-ledger", "XP-1", *db, "--dry-run") == 0
    assert "would append 0 adjust row(s)" in capsys.readouterr().out
    # a store that is not this experiment's arm is refused
    other = tmp_path / "plain.db"
    _db(other).close()
    assert _arc("experiment", "repair-ledger", "XP-1", *db, "--arm-db", str(other)) == 2
