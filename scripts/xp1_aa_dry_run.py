"""XP-1 A/A dry run on scratch stores (PLAN D44, card E10.4). No broker, no orders.

    .venv/bin/python scripts/xp1_aa_dry_run.py --dir <scratch> [--sessions 10]
        [--inject-usd 0] [--seed 7] [--write-doc]

End to end through the real CLI and the real arm runner, on fixtures:

1. ``arc experiment create`` + ``register`` the committed A/A spec
   (``config/experiments/live/xp1_aa_baseline.yaml``) on a fresh control store;
2. ``arc experiment start XP-1 --fixtures --arm-dir`` (t0 at the fixture clock
   minus one hour; virtual account at the fixture account's equity);
3. one fixture loop chain on control (``arc propose --fixtures``) and its paired
   copy on the arm (``arc experiment pair --fixtures``): the paired manifests and
   decisions the LLM-divergence rate is computed from;
4. *sessions* simulated sessions on both arms: each store gets the EOD
   ``pnl_snapshots`` row its reconcile writes (control: broker ``equity``; arm:
   ``virtual_equity``), plus open executions and closed outcomes with slippage,
   as the ladder and the journal write them. Control's daily P&L is seeded noise;
   the treatment's is control's plus small execution noise (identical configs),
   plus ``--inject-usd`` per day (a broken harness: must end ``invalid``);
5. ``arc experiment evaluate XP-1`` (stores the report, applies the verdict) and
   ``arc experiment report XP-1 --stored --format md``.

``--write-doc`` writes the committed example ``docs/RESEARCH/experiments/XP-1-aa.md``
(header: pending live run, E10.8). The scratch dir must not exist or be empty;
this script never opens ``data/arc.db``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from arc.context.ttl import to_db
from arc.pipeline.env import FIXTURE_NOW
from arc.store.db import DEFAULT_DB_PATH, connect
from arc.utils.calendar import next_session, previous_session, session_close

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "config" / "experiments" / "live" / "xp1_aa_baseline.yaml"
DOC = REPO / "docs" / "RESEARCH" / "experiments" / "XP-1-aa.md"
EID = "XP-1"
T0 = FIXTURE_NOW - dt.timedelta(hours=1)  # before the fixture loop chain (max_lag)
T0_EQUITY = 100_000.0  # arc/pipeline/fixtures/account.json
CTRL_SD = 300.0  # control's daily P&L sd ($)
EXEC_SD = 40.0  # identical configs, two accounts: fill-noise sd of the difference ($)
ORDERS_PER_DAY = 3


def _arc(*argv: str) -> tuple[int, str]:
    from arc.cli import main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(list(argv))
    return code, buf.getvalue()


def _check(code: int, out: str, what: str) -> str:
    if code != 0:
        msg = f"{what} failed (exit {code}): {out[-2000:]}"
        raise RuntimeError(msg)
    return out


def _eod(day: dt.date) -> dt.datetime:
    return session_close(day) + dt.timedelta(minutes=30)


def _pnl_row(conn: Any, day: dt.date, equity: float, *, arm: bool) -> None:
    details = {"day": day.isoformat(), "equity": f"{equity:.2f}"}
    if arm:  # the reconcile under a VirtualBroker (E10.2): equity == virtual_equity
        details["virtual_equity"] = f"{equity:.2f}"
    conn.execute(
        """INSERT INTO pnl_snapshots (id, snapshot_at, realized, unrealized, total, details_json)
           VALUES (?, ?, '0', '0', '0', ?)""",
        (uuid.uuid4().hex, to_db(_eod(day)), json.dumps(details)),
    )


def _trade(conn: Any, day: dt.date, *, filled: bool, slippage_bps: float, pnl: float) -> None:
    """One open execution (+ its outcome when filled), as the ladder and journal write them."""
    h = uuid.uuid4().hex
    at = to_db(_eod(day) - dt.timedelta(hours=4))
    conn.execute(
        """INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at)
           VALUES (?, 'SPY', 'bullish', 'momentum', 0.5, ?)""",
        (h, at),
    )
    conn.execute(
        """INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,
               quant_json, sizing_json, expires_at, created_at, regime)
           VALUES (?, ?, ?, '{"kind": "vertical_debit"}', 'dry run', '{}', '{}', ?, ?, 'bull')""",
        (h, h, h, at, at),
    )
    conn.execute(
        """INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo,
               band_hi, max_steps, attempts, contracts, filled_qty, fill_price,
               started_at, finished_at)
           VALUES (?, 'open', ?, 'arc2', '1.00', '1.20', 4, ?, 1, ?, ?, ?, ?)""",
        (
            h,
            "filled" if filled else "cancelled",
            2 if filled else 4,
            1 if filled else 0,
            "1.10" if filled else None,
            at,
            to_db(_eod(day) - dt.timedelta(hours=3)),
        ),
    )
    if filled:
        conn.execute(
            """INSERT INTO outcomes (id, proposal_hash, status, contracts, slippage_bps,
                   realised_pnl, at)
               VALUES (?, ?, 'closed', 1, ?, ?, ?)""",
            (uuid.uuid4().hex, h, slippage_bps, f"{pnl:.2f}", to_db(_eod(day))),
        )


def _sessions(first: dt.date, n: int) -> list[dt.date]:
    out = [first]
    while len(out) < n:
        out.append(next_session(out[-1]))
    return out


def simulate(
    control: Path, arm: Path, *, sessions: int, inject_usd: float, seed: int
) -> list[dt.date]:
    """Write *sessions* paired EOD sessions (and trades) into both stores."""
    rng = np.random.default_rng(seed)
    first = next_session(T0.date())  # t0 is mid-session: the next session is the first
    days = _sessions(first, sessions)
    ctrl_pnl = rng.normal(0.0, CTRL_SD, sessions)
    treat_pnl = ctrl_pnl + rng.normal(0.0, EXEC_SD, sessions) + inject_usd
    c_db, a_db = connect(control), connect(arm)
    try:
        _pnl_row(c_db, previous_session(first), T0_EQUITY, arm=False)  # control's t0 close
        c_eq = a_eq = T0_EQUITY
        for i, day in enumerate(days):
            c_eq += float(ctrl_pnl[i])
            a_eq += float(treat_pnl[i])
            _pnl_row(c_db, day, c_eq, arm=False)
            _pnl_row(a_db, day, a_eq, arm=True)
            for k in range(ORDERS_PER_DAY):
                # the two accounts fill slightly differently: control misses 1 in 10
                # orders, the experiment account 1 in 15; slippage a little wider on it
                n = i * ORDERS_PER_DAY + k
                _trade(c_db, day, filled=n % 10 != 9, slippage_bps=float(rng.normal(6, 2)),
                       pnl=float(rng.normal(0, 80)))  # fmt: skip
                _trade(a_db, day, filled=n % 15 != 14, slippage_bps=float(rng.normal(8, 2)),
                       pnl=float(rng.normal(0, 80)))  # fmt: skip
        c_db.commit()
        a_db.commit()
    finally:
        c_db.close()
        a_db.close()
    return days


def run(
    scratch: Path, *, sessions: int = 10, inject_usd: float = 0.0, seed: int = 7
) -> dict[str, Any]:
    """The whole dry run in *scratch*; returns the paths, the report and its Markdown."""
    from arc.control.effective import effective_settings, experiments_config
    from arc.experiments.calibration import calibration_markdown
    from arc.experiments.evaluate import latest_report
    from arc.experiments.runner import arm_stores

    scratch = scratch.resolve()
    if scratch.exists() and any(scratch.iterdir()):
        msg = f"{scratch} is not empty: the dry run always starts from fresh stores"
        raise SystemExit(msg)
    control = scratch / "arc.db"
    if control.resolve() == DEFAULT_DB_PATH.resolve():  # pragma: no cover - defensive
        raise SystemExit("refusing to touch data/arc.db")
    db = ("--db", str(control))
    _check(*_arc("experiment", "create", "--spec", str(SPEC), *db), "create")
    _check(*_arc("experiment", "register", EID, *db), "register")
    conn = connect(control)
    defaults_before = experiments_config(effective_settings(conn)).defaults
    conn.close()
    _check(*_arc("experiment", "start", EID, "--fixtures", "--arm-dir", str(scratch / "arms"),
                 "--now", T0.isoformat(), *db), "start")  # fmt: skip
    # one fixture loop chain on control, then its paired copy on the arm
    _check(*_arc("propose", "--fixtures", "--fixture-set", "bullish", "--profile", "cash_debit",
                 "--no-slack", "--lock-dir", str(scratch / "locks"), *db), "propose")  # fmt: skip
    conn = connect(control)
    chain = conn.execute(
        """SELECT chain_run_id FROM routine_runs WHERE job = 'research'
           AND chain_run_id IS NOT NULL ORDER BY rowid DESC LIMIT 1"""
    ).fetchone()[0]
    arm = arm_stores(conn, EID)["treatment"]
    conn.close()
    _check(*_arc("experiment", "pair", chain, "--fixtures", "--fixture-set", "bullish",
                 "--profile", "cash_debit", *db), "pair")  # fmt: skip
    days = simulate(control, arm, sessions=sessions, inject_usd=inject_usd, seed=seed)
    now = (_eod(days[-1]) + dt.timedelta(minutes=10)).isoformat()  # 16:40 ET evaluation
    _check(*_arc("experiment", "evaluate", EID, "--now", now, *db), "evaluate")
    text = _check(*_arc("experiment", "report", EID, "--stored", *db), "report")
    conn = connect(control)
    report = latest_report(conn, EID)
    defaults_after = experiments_config(effective_settings(conn)).defaults
    status = conn.execute(
        "SELECT status, reason FROM experiment_events WHERE experiment_id = ? ORDER BY id DESC",
        (EID,),
    ).fetchone()
    conn.close()
    assert report is not None  # noqa: S101 - evaluate just stored it
    return {
        "control": control,
        "arm": arm,
        "chain": chain,
        "days": days,
        "report": report,
        "text": text,
        "markdown": "\n".join(calibration_markdown(report)),
        "status": (status[0], status[1]),
        "defaults_before": defaults_before,
        "defaults_after": defaults_after,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--dir", required=True, help="Fresh scratch dir (created; must be empty)")
    p.add_argument("--sessions", type=int, default=10)
    p.add_argument("--inject-usd", type=float, default=0.0, help="Treatment edge per day ($)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--write-doc", action="store_true", help=f"Write {DOC.relative_to(REPO)}")
    args = p.parse_args(argv)
    res = run(Path(args.dir), sessions=args.sessions, inject_usd=args.inject_usd, seed=args.seed)
    sys.stdout.write(res["text"])
    sys.stdout.write("\n" + res["markdown"] + "\n")
    sys.stdout.write(f"\nexperiment status: {res['status'][0]} ({res['status'][1]})\n")
    if args.write_doc:
        from arc.experiments.calibration import aa_document

        DOC.parent.mkdir(parents=True, exist_ok=True)
        DOC.write_text(aa_document(res["report"], example=True))
        sys.stdout.write(f"wrote {DOC.relative_to(REPO)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
