"""E11.3 / D70: the propose step clamps live opens to the live cap (under D18)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from arc.config import ArcSettings
from arc.pipeline.runner import fixture_run
from arc.routines.config import load_routines
from arc.store.identity import read_store_env

if TYPE_CHECKING:
    import sqlite3

_FAKE_LIVE = Path(__file__)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arc.config._LIVE_ENV_PATH", _FAKE_LIVE)
    monkeypatch.delenv("ARC_DB_PATH", raising=False)


def _sizing(conn: sqlite3.Connection) -> tuple[int, dict[str, object]]:
    row = conn.execute(
        "SELECT payload FROM decisions WHERE stage = 'sizing' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row[0])
    return int(payload["contracts"]), payload


def _n(prop: dict[str, str]) -> int:
    return int(json.loads(prop["sizing_json"])["contracts"])


def _run(env: str, **kw: object) -> tuple[sqlite3.Connection, object]:
    s = ArcSettings(  # type: ignore[call-arg]
        _env_file=None, env=env, account_profile="margin", **kw
    )
    return fixture_run(s, load_routines())


def test_propose_clamps_to_live_cap() -> None:
    paper_conn, paper_rep = _run("paper")
    (paper_prop,) = paper_rep.proposals  # type: ignore[attr-defined]
    assert _n(paper_prop) > 1  # D18 alone sizes the fixture above 1
    n, payload = _sizing(paper_conn)
    assert n == _n(paper_prop) and "live_cap" not in payload

    live_conn, live_rep = _run("live")
    ident = read_store_env(live_conn)
    assert ident is not None and ident.env == "live"
    (live_prop,) = live_rep.proposals  # type: ignore[attr-defined]
    assert _n(live_prop) == 1
    n, payload = _sizing(live_conn)
    assert n == 1 and payload["live_cap"] == 1 and payload["code"] == "live_capped"
    paper_total = Decimal(str(_sizing(paper_conn)[1]["max_loss_total"]))
    per_contract = paper_total / _n(paper_prop)
    assert Decimal(str(payload["max_loss_total"])) == per_contract  # one contract's risk


def test_live_cap_configurable() -> None:
    conn, rep = _run("live", live_max_contracts_until_gate=2)
    (prop,) = rep.proposals  # type: ignore[attr-defined]
    assert _n(prop) == 2
    assert _sizing(conn)[1]["live_cap"] == 2


def test_swap_open_clamps_to_live_cap() -> None:
    """The swap open path applies the same clamp (``arc/positions/steps.py``)."""
    import inspect

    from arc.positions import steps

    src = inspect.getsource(steps)
    assert "apply_live_cap(size, live_size_cap(settings, account), info.equity)" in src
    assert "live_gate_met=live_gate_status(ctx.conn, settings, now=ctx.now)" in src
