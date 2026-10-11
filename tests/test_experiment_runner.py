"""E10.2 arm runner, end to end on fixture stores: t0, paired chains (shared inputs,
fork step, manifests), N arms by configuration, the virtual account against a big
paper account, settled cash through the gate, overlay reaching the consumers, the
read-time union the evaluator reads, and the tick's detached spawn."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.broker.base import AccountInfo, BrokerPosition, Fill
from arc.experiments.arms import ArmIdentity, read_identity, write_identity
from arc.experiments.config import ArmRunner, RunnerConfig
from arc.experiments.paired import attach_arms, paired_view
from arc.experiments.runner import ArmStartError, arm_stores, start_arms
from arc.experiments.virtual import (
    open_account,
    record_fills,
    replay,
    rows,
    virtual_account,
)
from arc.pipeline.env import FIXTURE_NOW
from arc.pipeline.market import settled_cash
from arc.sizing import size_contracts
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

T0 = FIXTURE_NOW - dt.timedelta(hours=1)
SPEC = "config/experiments/live/xp1_aa_baseline.yaml"


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def _arc(*argv: str) -> int:
    from arc.cli import main

    return main(list(argv))


def _registered(db: Path) -> None:
    assert (
        _arc("experiment", "create", "--owner-approval", "P-1", "--spec", SPEC, "--db", str(db))
        == 0
    )
    assert _arc("experiment", "register", "--owner-approval", "P-1", "XP-1", "--db", str(db)) == 0


def _runner(*names: str) -> RunnerConfig:
    arms = {
        "treatment": ArmRunner(
            spec_arm="treatment", keys_env="ALPACA_EXP", db="exp-{experiment_id}.db"
        ),
        "shadow_control": ArmRunner(
            spec_arm="control", keys_env="ALPACA_SHADOW", db="shadow-{experiment_id}.db"
        ),
    }
    return RunnerConfig(arms={n: arms[n] for n in names})


# --- t0 + pairing on fixtures ------------------------------------------------------


@pytest.fixture
def control(tmp_path: Path) -> Path:
    db = tmp_path / "control.db"
    _registered(db)
    return db


def test_n_arms_by_configuration_pair_one_control_chain(control: Path, tmp_path: Path) -> None:
    """control (the normal store) + shadow control + treatment: config only, no code change."""
    conn = _db(control)
    st = start_arms(
        conn,
        "XP-1",
        actor="local",
        now=T0,
        t0_equity=D(10000),
        runner=_runner("treatment", "shadow_control"),
        arm_dir=tmp_path / "arms",
        control_sha="abcdef1",
    )
    assert st.status.value == "running" and st.running is not None
    assert st.running.t0_equity == 10000
    stores = arm_stores(conn, "XP-1")
    assert set(stores) == {"treatment", "shadow_control"}
    assert set(arm_stores(conn)) == {"XP-1:treatment", "XP-1:shadow_control"}
    conn.close()

    # control's loop (Scalp + Research chain), then each arm's paired copy
    assert (
        _arc("propose", "--fixtures", "--fixture-set", "bullish", "--profile", "cash_debit",
             "--db", str(control), "--no-slack", "--lock-dir", str(tmp_path / "locks"))
        == 0
    )  # fmt: skip
    conn = _db(control)
    chain = conn.execute(
        "SELECT chain_run_id FROM routine_runs WHERE job = 'research' AND chain_run_id IS NOT NULL"
    ).fetchone()[0]
    assert (
        _arc("experiment", "pair", chain, "--fixtures", "--fixture-set", "bullish",
             "--profile", "cash_debit", "--db", str(control))
        == 0
    )  # fmt: skip
    control_runs = {
        r["job"]: r["run_id"] for r in conn.execute("SELECT job, run_id FROM routine_runs")
    }
    for name, path in stores.items():
        arm = _db(path)
        ident = read_identity(arm)
        assert ident is not None and ident.arm_id == f"XP-1:{name}"
        runs = arm.execute(
            "SELECT job, status, summary FROM routine_runs ORDER BY step_index"
        ).fetchall()
        jobs = [r["job"] for r in runs]
        # shared inputs: the Scalp ran once, in control; the arm never re-runs it
        assert "scalp" not in jobs
        assert jobs == [
            "research", "exits.mandatory", "quant.exit", "risk.exit", "quant.open",
            "risk.open", "quant.revise", "quant.propose", "broker.execute",
        ]  # fmt: skip
        assert all(r["status"] in ("ok", "skipped") for r in runs)
        # E13.15: exits.mandatory reads the arm's own book, so the fork is right after
        # Research; only Research's outputs are control's, reused verbatim
        fork = "exits.mandatory"
        for r in runs[: jobs.index(fork)]:
            assert r["summary"].startswith(f"paired: reused {control_runs[r['job']]}")
        pair = arm.execute("SELECT * FROM arm_pairs").fetchone()
        assert pair["status"] == "ok" and pair["fork_step"] == fork
        assert pair["arm_chain_run_id"] == f"{chain}.{name}"
        manifests = arm.execute(
            "SELECT arm_id, payload FROM run_manifests ORDER BY created_at"
        ).fetchall()
        assert manifests, "the arm's own steps leave manifests"
        for m in manifests:
            p = json.loads(m["payload"])
            assert m["arm_id"] == f"XP-1:{name}"
            assert p["paired_chain_run_id"] == chain and p["fork_step"] == fork
            assert p["git_sha"]
        # Research's decisions are control's, under the arm's chain id
        assert (
            arm.execute(
                "SELECT count(*) FROM decisions WHERE chain_run_id = ?", (f"{chain}.{name}",)
            ).fetchone()[0]
            > 0
        )
        arm.close()
    # pairing the same chain again is a no-op (duplicate), never a second run
    assert (
        _arc("experiment", "pair", chain, "--fixtures", "--fixture-set", "bullish",
             "--profile", "cash_debit", "--db", str(control))
        == 0
    )  # fmt: skip
    arm = _db(stores["treatment"])
    assert (
        arm.execute("SELECT count(*) FROM routine_runs WHERE job = 'quant.propose'").fetchone()[0]
        == 1
    )
    arm.close()


def test_pair_skips_a_stale_control_chain(control: Path, tmp_path: Path) -> None:
    from arc.control.effective import effective_routines
    from arc.experiments.runner import pair_chain

    conn = _db(control)
    start_arms(
        conn,
        "XP-1",
        actor="local",
        now=T0,
        t0_equity=D(10000),
        runner=_runner("treatment"),
        arm_dir=tmp_path / "arms",
        control_sha="abcdef1",
    )
    conn.close()
    assert _arc("propose", "--fixtures", "--fixture-set", "bullish", "--db", str(control),
                "--no-slack", "--lock-dir", str(tmp_path / "locks")) == 0  # fmt: skip
    conn = _db(control)
    chain = conn.execute(
        "SELECT chain_run_id FROM routine_runs WHERE job = 'research' AND chain_run_id IS NOT NULL"
    ).fetchone()[0]
    arm = _db(arm_stores(conn, "XP-1")["treatment"])
    res = pair_chain(
        conn,
        arm,
        chain,
        routines=effective_routines(arm),
        runner=_runner("treatment"),
        now=FIXTURE_NOW + dt.timedelta(minutes=10),
    )
    assert res.status == "skipped" and "max_lag_seconds" in res.reason
    assert arm.execute("SELECT count(*) FROM routine_runs").fetchone()[0] == 0


def _gate_rows(c: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {
            "ticker": r["ticker"],
            "passed": r["passed"],
            "ref_time": r["ref_time"],
            "violations": json.loads(r["violations_json"]),
            "decided_at": r["decided_at"],
        }
        for r in c.execute(
            """SELECT p.ticker, g.passed, g.ref_time, g.violations_json, g.decided_at
               FROM gate_decisions g JOIN proposals p USING (proposal_hash)
               ORDER BY p.ticker, g.decided_at"""
        )
    ]


@pytest.mark.parametrize("profile", [None, "cash_debit"])
def test_arm_paired_late_gates_replayed_quotes_as_control_did(
    control: Path, tmp_path: Path, profile: str | None
) -> None:
    """E10.2d: the arm pairs 120 s after control and replays control's tape.

    The recorded fixture chain's quotes were 1-43 s old at control's clock; at the
    arm's clock they are 121-163 s old (> quote_max_age_seconds 60), the XP-10
    stale_data bias. Aged against the paired chain's clock the arm's gate decides
    exactly as control's did, and every arm decision records ``paired_chain``.
    """
    conn = _db(control)
    stores = start_arms(
        conn, "XP-1", actor="local", now=T0, t0_equity=D(100000),  # = the fixture account
        runner=_runner("treatment"), arm_dir=tmp_path / "arms", control_sha="abcdef1",
    )  # fmt: skip
    assert stores.running is not None
    # The fixture SPY debit vertical sits under the live Net EV floor; switch the floor
    # off (D26 override on control, which every arm store reads) so it reaches the gate.
    from arc.config import ArcSettings
    from arc.control.service import ControlService

    owner = "U0OWNER001"
    svc = ControlService(
        conn,
        base=ArcSettings(_env_file=None, approver_slack_user_ids=[owner]),  # type: ignore[call-arg]
        now=lambda: T0,
        is_halted=lambda: False,
    )
    r = svc.set("entries.net_ev_floor_live", "off", actor=owner, source="slack")
    assert r.pending is not None
    svc.confirm(r.pending.code, actor=owner, source="slack")
    conn.close()
    prof = ["--profile", profile] if profile else []
    assert _arc("propose", "--fixtures", "--fixture-set", "bullish", *prof, "--db",
                str(control), "--no-slack", "--lock-dir", str(tmp_path / "locks")) == 0  # fmt: skip
    conn = _db(control)
    chain = conn.execute(
        "SELECT chain_run_id FROM routine_runs WHERE job = 'research' AND chain_run_id IS NOT NULL"
    ).fetchone()[0]
    assert (
        conn.execute(
            "SELECT count(*) FROM market_tape WHERE chain_run_id = ?", (chain,)
        ).fetchone()[0]
        > 0
    ), "control's fixture loop records its market reads"
    late = (FIXTURE_NOW + dt.timedelta(seconds=120)).isoformat()
    assert _arc("experiment", "pair", chain, "--fixtures", "--fixture-set", "bullish", *prof,
                "--now", late, "--db", str(control)) == 0  # fmt: skip
    arm = _db(arm_stores(conn, "XP-1")["treatment"])
    ctl_rows, arm_rows = _gate_rows(conn), _gate_rows(arm)
    assert ctl_rows and len(arm_rows) == len(ctl_rows)
    assert {r["ref_time"] for r in ctl_rows} == {"wall"}
    assert {r["ref_time"] for r in arm_rows} == {"paired_chain"}
    for c, a in zip(ctl_rows, arm_rows, strict=True):
        assert (a["ticker"], a["passed"], a["violations"]) == (
            c["ticker"], c["passed"], c["violations"],
        )  # fmt: skip
        assert not any("stale_data" in v for v in a["violations"])  # type: ignore[union-attr]
    stale = arm.execute(
        "SELECT count(*) FROM decisions WHERE reason_code = 'gate:stale_data'"
    ).fetchone()[0]
    assert stale == 0
    arm.close()


def test_start_refuses_existing_store_and_leaves_nothing(control: Path, tmp_path: Path) -> None:
    conn = _db(control)
    arms = tmp_path / "arms"
    arms.mkdir()
    (arms / "exp-XP-1.db").write_text("")
    with pytest.raises(ArmStartError, match="already exists"):
        start_arms(
            conn,
            "XP-1",
            actor="local",
            now=T0,
            t0_equity=D(10000),
            runner=_runner("treatment"),
            arm_dir=arms,
            control_sha="abcdef1",
        )
    assert arm_stores(conn) == {}
    from arc.experiments.store import ExperimentStore

    assert ExperimentStore(conn).require("XP-1").status.value == "registered"

    def not_flat(arm: ArmRunner) -> None:
        raise ArmStartError(f"{arm.keys_env} account holds 1 position(s)")

    with pytest.raises(ArmStartError, match="holds 1 position"):
        start_arms(
            conn,
            "XP-1",
            actor="local",
            now=T0,
            t0_equity=D(10000),
            runner=_runner("shadow_control"),
            arm_dir=arms,
            check_flat=not_flat,
            control_sha="abcdef1",
        )
    assert not (arms / "shadow-XP-1.db").exists()


# --- virtual account vs a big paper account -----------------------------------------


def _paper(equity: str = "95000") -> AccountInfo:
    return AccountInfo(
        account_id="exp",
        equity=D(equity),
        buying_power=D(equity) * 4,  # paper: 4x margin
        cash=D(equity),
        options_buying_power=D(equity) * 2,
        non_marginable_buying_power=D(equity),
        options_approved_level=3,
    )


NOW = dt.datetime(2026, 10, 6, 10, 0, tzinfo=ET)  # Tuesday
AID = "XP-1:treatment"


def _fill(side: str, px: str, at: dt.datetime, oid: str, qty: int = 1) -> Fill:
    return Fill(
        broker_order_id=oid,
        symbol="SPY261120C00580000",
        side=side,
        qty=D(qty),
        price=D(px),
        filled_at=at,
    )


def test_95k_paper_account_trades_as_the_10k_control() -> None:
    c = _db()
    open_account(c, AID, t0_equity=D(10000), legacy={}, at=NOW)
    info = virtual_account(
        replay(rows(c, AID), as_of=NOW.date()), _paper(), [], cash_settlement=True
    )
    assert info.equity == 10000 and info.buying_power == 10000
    assert info.options_buying_power == 10000 and settled_cash(info) == 10000
    # the reserved excess ($85k) is never usable: sizing caps on virtual equity
    sized = size_contracts(
        suggestion=20, max_loss_per_contract=D(100), equity=info.equity, cap_pct=0.05
    )
    assert sized.contracts == 5 and sized.max_loss_total == D(500)  # 5% of $10k


def test_virtual_equity_moves_only_with_the_arms_own_pnl() -> None:
    c = _db()
    open_account(c, AID, t0_equity=D(10000), legacy={}, at=NOW)
    record_fills(c, AID, [_fill("buy", "2.00", NOW + dt.timedelta(minutes=5), "o1")])
    pos = BrokerPosition(
        symbol="SPY261120C00580000", qty=D(1), side="long", market_value=D(250),
        unrealized_pl=D(50),
    )  # fmt: skip
    # the paper account's own equity swings (other money, other legs) change nothing
    for broker_equity in ("95000", "120000", "40000"):
        info = virtual_account(
            replay(rows(c, AID), as_of=NOW.date()), _paper(broker_equity), [pos],
            cash_settlement=True,
        )  # fmt: skip
        assert info.equity == D(10050)  # 10000 - 200 paid + 250 mark
        assert info.cash == D(9800)


def test_settled_cash_rejection_under_a_cash_profile() -> None:
    """Same-day sale proceeds settle T+1 (injected now): the gate's cash rule rejects them."""
    from tests.test_account_profiles import acct, bull_call, cfg, codes, gate

    c = _db()
    open_account(c, AID, t0_equity=D(1000), legacy={}, at=NOW)
    record_fills(
        c,
        AID,
        [
            _fill("buy", "5.00", NOW + dt.timedelta(minutes=1), "o1"),
            _fill("sell", "5.00", NOW + dt.timedelta(minutes=30), "o2"),
        ],
    )
    today = virtual_account(
        replay(rows(c, AID), as_of=NOW.date()), _paper(), [], cash_settlement=True
    )
    assert today.cash == 1000 and settled_cash(today) == 500  # $500 proceeds unsettled
    # bull call: $800.20 with fees -> rejected on unsettled cash today ...
    d = gate(bull_call(), cfg("cash_debit"), acct(str(settled_cash(today))))
    assert codes(d) == {"account_profile_settled_cash"}
    # ... allowed the next session, when the proceeds have settled
    nxt = virtual_account(
        replay(rows(c, AID), as_of=dt.date(2026, 10, 7)), _paper(), [], cash_settlement=True
    )
    assert settled_cash(nxt) == 1000
    assert gate(bull_call(), cfg("cash_debit"), acct(str(settled_cash(nxt)))).passed
    # a margin control profile: no settlement tracking, spendable cash is usable
    margin = virtual_account(
        replay(rows(c, AID), as_of=NOW.date()), _paper(), [], cash_settlement=False
    )
    assert settled_cash(margin) == 1000


def test_legacy_reservation_never_usable_until_released() -> None:
    from arc.experiments.virtual import release_legacy

    c = _db()
    open_account(c, AID, t0_equity=D(10000), legacy={"os-1": D(400), "os-2": D(600)}, at=NOW)
    info = virtual_account(
        replay(rows(c, AID), as_of=NOW.date()), _paper(), [], cash_settlement=True
    )
    assert info.equity == 10000 and info.buying_power == 9000
    release_legacy(c, AID, ["os-2"], at=NOW)
    info = virtual_account(
        replay(rows(c, AID), as_of=NOW.date()), _paper(), [], cash_settlement=True
    )
    assert info.buying_power == 9600


# --- overlay reaches the consumers ----------------------------------------------------


def _arm_store(tmp_path: Path, overlay: dict) -> sqlite3.Connection:
    ctl = tmp_path / "control.db"
    _db(ctl).close()
    arm = _db(tmp_path / "arm.db")
    write_identity(
        arm,
        ArmIdentity(
            arm_id=AID, experiment_id="XP-1", arm="treatment", spec_arm="treatment",
            keys_env="ALPACA_EXP", control_db=str(ctl), overlay=overlay, created_at=NOW,
        ),
    )  # fmt: skip
    return arm


def test_overlay_reaches_exits_and_costs_via_effective(tmp_path: Path) -> None:
    from arc.control.effective import cost_model, effective_settings, exit_config

    base = exit_config(effective_settings(_db()))
    arm = _arm_store(tmp_path, {"exits": {"default": {"take_profit_pct_of_debit": 0.5}}})
    s = effective_settings(arm)
    assert exit_config(s).default.take_profit_pct_of_debit == 0.5
    assert base.default.take_profit_pct_of_debit != 0.5  # control unchanged
    # untouched targets equal control's
    assert cost_model(s) == cost_model(effective_settings(_db()))


# --- the evaluator's read-time union --------------------------------------------------


def _pnl(conn: sqlite3.Connection, day: str, details: dict) -> None:
    conn.execute(
        """INSERT INTO pnl_snapshots (id, snapshot_at, realized, unrealized, total, details_json)
           VALUES (?, ?, '0', '0', '0', ?)""",
        (f"p-{day}-{len(details)}", f"{day}T20:00:00.000000Z", json.dumps({"day": day, **details})),
    )
    conn.commit()


def test_paired_view_projects_arm_id_for_the_evaluator(tmp_path: Path) -> None:
    from arc.experiments.evaluate import TREATMENT_EQUITY_FIELD, _eod_equity
    from arc.routines.runs import RoutineStateRepo

    ctl = _db(tmp_path / "control.db")
    arm = _arm_store(tmp_path, {})
    _pnl(ctl, "2026-10-06", {"equity": "10100"})
    _pnl(arm, "2026-10-06", {"equity": "10050", TREATMENT_EQUITY_FIELD: "10050"})
    arm.close()
    # no arm store recorded: the connection is returned unchanged
    with paired_view(ctl) as v:
        assert v is ctl
    RoutineStateRepo(ctl).set("experiment_arm:treatment", str(tmp_path / "arm.db"), now=NOW)
    with paired_view(ctl) as v:
        assert v is not ctl
        assert _eod_equity(v, None) == {dt.date(2026, 10, 6): 10100.0}
        assert _eod_equity(v, AID, TREATMENT_EQUITY_FIELD) == {dt.date(2026, 10, 6): 10050.0}
        with pytest.raises(sqlite3.OperationalError):  # read-only
            v.execute("INSERT INTO halts (reason, actor, at) VALUES ('x', 'y', 'z')")
    # the control store itself holds no arm rows (nothing mirrored)
    assert (
        ctl.execute("SELECT count(*) FROM pnl_snapshots WHERE arm_id IS NOT NULL").fetchone()[0]
        == 0
    )


def test_attach_skips_a_store_without_identity(tmp_path: Path) -> None:
    ctl = _db(tmp_path / "control.db")
    _db(tmp_path / "plain.db").close()
    assert attach_arms(ctl, [tmp_path / "plain.db"]) == {}


def test_reconcile_writes_virtual_equity_for_a_virtual_broker(tmp_path: Path) -> None:
    """The arm's EOD pnl row carries details_json.virtual_equity (E10.3 reads only that)."""
    from typing import cast

    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings
    from arc.experiments.broker import virtual_broker
    from arc.reconcile.engine import reconcile

    class _Paper:
        def account(self) -> AccountInfo:
            return _paper()

        def positions(self) -> list[BrokerPosition]:
            return []

        def fills(self, since: dt.datetime) -> list[Fill]:
            _ = since
            return []

        def option_orders_since(self, since: dt.datetime) -> list:
            _ = since
            return []

    arm = _arm_store(tmp_path, {})
    open_account(arm, AID, t0_equity=D(10000), legacy={}, at=NOW)
    ident = read_identity(arm)
    assert ident is not None
    s = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
    eod = NOW.replace(hour=16, minute=5)
    vb = virtual_broker(arm, ident, cast("BrokerAdapter", _Paper()), settings=s, now=lambda: eod)
    reconcile(arm, cast("BrokerAdapter", vb), settings=s, now=eod, halt=False)
    d = json.loads(arm.execute("SELECT details_json FROM pnl_snapshots").fetchone()[0])
    assert d["day"] == "2026-10-06"
    assert D(d["virtual_equity"]) == 10000 and D(d["equity"]) == 10000  # not $95k
    # control (a plain broker) writes no virtual_equity key
    ctl = _db()
    reconcile(ctl, cast("BrokerAdapter", _Paper()), settings=s, now=eod, halt=False)
    d = json.loads(ctl.execute("SELECT details_json FROM pnl_snapshots").fetchone()[0])
    assert "virtual_equity" not in d and d["equity"] == "95000"


# --- the tick's detached spawn ---------------------------------------------------------


def test_tick_spawns_arms_only_while_an_experiment_runs(
    control: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arc.experiments import cli_arms
    from arc.routines.handlers import RunEnv

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "arc.routines.spawn.spawn_detached", lambda argv, env=None: spawned.append(argv) or 42
    )
    conn = _db(control)
    env = RunEnv(db_path=str(control), config_path=None, lock_dir=str(tmp_path / "l"), slack=False)
    assert cli_arms.spawn_arms_tick(conn, env) is None  # registered, not running
    start_arms(
        conn,
        "XP-1",
        actor="local",
        now=T0,
        t0_equity=D(10000),
        runner=_runner("treatment"),
        arm_dir=tmp_path / "arms",
        control_sha="abcdef1",
    )
    assert cli_arms.spawn_arms_tick(conn, env) == 42
    argv = spawned[-1]
    assert argv[argv.index("experiment") + 1] == "arms-tick"
    assert "--db" in argv and str(control) in argv
