"""Synthetic N-proposal store for the Trades list performance check (E8.7b)."""

from __future__ import annotations

import datetime as dt
import json
import random
import sqlite3
import time
from pathlib import Path

from arc.store.migrate import migrate

TICKERS = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMD", "META", "TSLA", "AMZN",
           "GOOGL", "XLE", "XLF", "DIA", "SMH", "TLT", "GLD", "NFLX", "CRM", "ORCL"]  # fmt: skip


def build_synthetic(path: Path, n: int, *, seed: int = 7) -> None:
    """Migrate *path* and bulk-insert *n* proposals with gate/approval/exec/mc rows."""
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    migrate(conn)
    t0 = dt.datetime(2025, 1, 2, 14, tzinfo=dt.UTC)
    props, gates, apps, execs, mcs, structs, cands = [], [], [], [], [], [], []
    for i in range(n):
        h = f"{i:064x}"
        at = t0 + dt.timedelta(minutes=7 * i)
        tk = TICKERS[i % len(TICKERS)]
        kind = "close" if i % 10 == 9 else "open"
        net = round(rng.uniform(0.5, 6.0), 2)
        st = {
            "kind": "vertical_debit" if i % 3 else "long_call",
            "net_debit_credit": str(net),
            "legs": [{"occ_symbol": f"{tk}261030C00100000", "side": "long", "ratio": 1}],
        }
        cands.append((f"c{i}", tk, "bullish", "earnings", 0.6, at.isoformat()))
        props.append(
            (
                f"p{i}",
                f"c{i}",
                h,
                json.dumps(st),
                "t",
                json.dumps({"pop": rng.random()}),
                "",
                json.dumps({"contracts": rng.randint(1, 5)}),
                at.isoformat(),
                at.isoformat(),
                f"run{i // 5}",
                at.date().isoformat(),
                tk,
                kind,
            )
        )
        passed = rng.random() > 0.2
        gates.append(
            (f"g{i}", h, int(passed), "[]" if passed else '["spread_too_wide: x"]', at.isoformat())
        )
        if passed:
            status = rng.choice(["approved", "rejected", "expired", "approved"])
            apps.append(
                (h, tk, at.date().isoformat(), "{}", status, at.isoformat(), at.isoformat())
            )
            if status == "approved":
                filled = rng.random() > 0.3
                execs.append(
                    (
                        h,
                        kind,
                        "filled" if filled else "cancelled",
                        "arc2",
                        "1",
                        "3",
                        3,
                        1,
                        2,
                        2 if filled else 0,
                        str(round(net + 0.02, 2)) if filled else None,
                        at.isoformat(),
                    )
                )
                if filled and kind == "open":
                    closed = rng.random() > 0.5
                    structs.append(
                        (
                            f"s{i}",
                            tk,
                            h,
                            f"c{i}",
                            json.dumps(st),
                            2,
                            str(net),
                            at.isoformat(),
                            "closed" if closed else "open",
                            str(-round(net * 1.3, 2)) if closed else None,
                            "take_profit" if closed else None,
                        )
                    )
        mcs.append(
            (
                f"m{i}",
                h,
                json.dumps(
                    {
                        "analytics": {
                            "account_profile": "cash_debit",
                            "exit_model": {
                                "managed": {"net_ev": rng.uniform(-20, 60), "pop": rng.random()},
                                "static": {"pop": rng.random()},
                            },
                        }
                    }
                ),
                at.isoformat(),
            )
        )
    conn.executemany(
        "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, "
        "created_at) VALUES (?,?,?,?,?,?)",
        cands,
    )
    conn.executemany(
        "INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, "
        "thesis, quant_json, risk_narrative, sizing_json, expires_at, created_at, "
        "run_id, day, ticker, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        props,
    )
    conn.executemany(
        "INSERT INTO gate_decisions (id, proposal_hash, passed, violations_json, "
        "decided_at) VALUES (?,?,?,?,?)",
        gates,
    )
    conn.executemany(
        "INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, "
        "status, channel, expires_at, created_at) VALUES (?,?,?,?,?,'log',?,?)",
        apps,
    )
    conn.executemany("INSERT INTO executions (proposal_hash, kind, status, token_version, band_lo, "
                     "band_hi, max_steps, attempts, contracts, filled_qty, fill_price, started_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                         (e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7], e[8], e[9], e[10], e[11])
                         for e in execs])  # fmt: skip
    conn.executemany(
        "INSERT INTO market_contexts (id, proposal_hash, payload, created_at) VALUES (?,?,?,?)", mcs
    )
    conn.executemany(
        "INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id, "
        "structure_json, contracts, entry_net, opened_at, status, close_net, "
        "exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        structs,
    )
    conn.commit()
    conn.execute("ANALYZE")
    conn.close()


if __name__ == "__main__":  # pragma: no cover - manual probe
    import sys

    out = Path(sys.argv[1])
    out.unlink(missing_ok=True)
    t = time.perf_counter()
    build_synthetic(out, int(sys.argv[2]) if len(sys.argv) > 2 else 100_000)
    print(f"built in {time.perf_counter() - t:.1f}s")  # noqa: T201
