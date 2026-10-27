"""E27.4 (D126): ``ml.score`` inside ``quant.open`` — shadow only, fail open.

The fixture pipeline (offline, canned personas) runs with the shadow step off and on
against a hand-built text-model bundle (no ``lightgbm``): prompts, the proposal and the
gate inputs are byte-identical, the ``ml_scores`` entry and the journal links appear,
the outcome row carries the copied score, and every failure leaves trading untouched.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from arc.journal.models import OutcomeRecord, OutcomeStatus
from arc.journal.store import JournalStore
from arc.ml.features.registry import _running_version
from arc.ml.scores import MlScore, legs_key
from arc.ml.shadow import m2_ranks
from arc.pipeline.env import FIXTURE_NOW
from tests.test_ml_serve import _bundle

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _models(tmp_path: Path) -> Path:
    """``<data>/ml/models/credit/m2-aaa`` registered at the running feature schema."""
    root = tmp_path / "ml" / "models"
    vdir = _bundle(root, "credit", schema=_running_version())
    (vdir / "features.json").write_text(json.dumps(["net_mid"]))
    from arc.ml.registry import hash_files

    man = json.loads((vdir / "manifest.json").read_text())
    man["files"] = hash_files(vdir, list(man["files"]))
    (vdir / "manifest.json").write_text(json.dumps(man))
    return tmp_path


def _run(
    monkeypatch: pytest.MonkeyPatch, *, on: bool, data: Path | None = None
) -> tuple[sqlite3.Connection, Any, dict[str, str]]:
    from tests.test_menu_measure import _run as run

    monkeypatch.setenv("ARC_ML_SHADOW", "true" if on else "false")
    if data is not None:
        monkeypatch.setenv("ARC_ML_DATA_DIR", str(data))
    return run("margin", monkeypatch, menu_measure="control")  # type: ignore[no-any-return]


def _entry(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, payload FROM context_entries WHERE kind = 'ml_scores' ORDER BY rowid DESC"
    ).fetchone()
    return None if row is None else {"id": row[0]} | json.loads(row[1])


def _decisions(conn: sqlite3.Connection, code: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM decisions WHERE reason_code = ? ORDER BY rowid", (code,)
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _strip_ml(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k not in ("ml", "ml_scores_id")} for r in rows]


def _proposals(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    q = "SELECT structure_json, quant_json, sizing_json, thesis FROM proposals ORDER BY rowid"
    return [tuple(r) for r in conn.execute(q)]


def _gate(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(gate_decisions)")}
    keep = [c for c in ("passed", "violations_json", "account_snapshot") if c in cols]
    q = f"SELECT {', '.join(keep)} FROM gate_decisions ORDER BY rowid"  # noqa: S608
    return [tuple(r) for r in conn.execute(q)]


def _alerts(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT key FROM ops_alerts WHERE key LIKE 'ml:%'")]


@pytest.fixture
def off(monkeypatch: pytest.MonkeyPatch) -> tuple[sqlite3.Connection, Any, dict[str, str]]:
    return _run(monkeypatch, on=False)


def test_shadow_on_scores_menu_and_changes_no_decision(
    off: tuple[sqlite3.Connection, Any, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    c_off, r_off, p_off = off
    c_on, r_on, p_on = _run(monkeypatch, on=True, data=_models(tmp_path))
    # prompts byte-identical: no persona sees a score
    assert p_off.keys() == p_on.keys()
    for persona in p_off:
        assert p_off[persona] == p_on[persona], persona
    # the same proposal (structure, quant metrics, sizing, thesis) and the same gate result
    # (proposal_hash carries a per-run nonce, so compare the content, not the hash)
    assert len(r_on.proposals) == len(r_off.proposals) == 1
    assert _proposals(c_off) == _proposals(c_on)
    assert _gate(c_off) == _gate(c_on)
    # off writes nothing
    assert _entry(c_off) is None
    assert all("ml_scores_id" not in d for d in _decisions(c_off, "proposed"))
    # on: one entry scoring every menu item
    e = _entry(c_on)
    assert e is not None
    assert e["status"] == "ok", e["reason"]
    assert e["model_version"] == "m2-aaa" and e["family"] == "credit"
    assert e["n"] == e["n_ok"] == len(e["scores"]) > 0
    n_menu = len(_decisions(c_on, "chosen_from_menu")) + len(_decisions(c_on, "menu_not_chosen"))
    assert e["n"] == n_menu
    for s in e["scores"]:
        assert s["key"][0] in "+-" and s["rank_live"] >= 1 and s["rank_m2"] >= 1
        assert 0.0 <= s["p_win"] <= 1.0
    # journal links: every menu decision names the entry and carries its own score
    keys = {s["key"]: s for s in e["scores"]}
    for code in ("chosen_from_menu", "menu_not_chosen", "proposed"):
        rows = _decisions(c_on, code)
        assert rows and all(d["ml_scores_id"] == e["id"] for d in rows)
        assert all(d["ml"]["model_version"] == "m2-aaa" for d in rows)
        assert _strip_ml(rows) == _strip_ml(_decisions(c_off, code))
    (prop,) = _decisions(c_on, "proposed")
    legs = [(lg["occ_symbol"], lg["side"]) for lg in prop["structure"]["legs"]]
    assert prop["ml"]["p_win"] == keys[legs_key(legs)]["p_win"]
    assert _alerts(c_on) == []


def test_outcome_copies_the_score_at_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    conn, _, _ = _run(monkeypatch, on=True, data=_models(tmp_path))
    phash = str(conn.execute("SELECT proposal_hash FROM proposals").fetchone()[0])
    (prop,) = _decisions(conn, "proposed")
    j = JournalStore(conn)
    j.record_outcome(
        OutcomeRecord(proposal_hash=phash, status=OutcomeStatus.NOT_TRADED, at=FIXTURE_NOW)
    )
    row = conn.execute(
        "SELECT ml_p_win, ml_e_r, ml_p_stop, ml_model_version FROM outcomes"
    ).fetchone()
    assert tuple(row) == (
        prop["ml"]["p_win"],
        prop["ml"]["e_r"],
        prop["ml"]["p_stop"],
        "m2-aaa",
    )
    back = j.outcome(phash)
    assert back is not None and back.ml_model_version == "m2-aaa"
    assert back.ml_p_win == prop["ml"]["p_win"]


def test_outcome_without_score_stays_null(
    off: tuple[sqlite3.Connection, Any, dict[str, str]],
) -> None:
    conn = off[0]
    phash = str(conn.execute("SELECT proposal_hash FROM proposals").fetchone()[0])
    JournalStore(conn).record_outcome(
        OutcomeRecord(proposal_hash=phash, status=OutcomeStatus.NOT_TRADED, at=FIXTURE_NOW)
    )
    row = conn.execute("SELECT ml_p_win, ml_model_version FROM outcomes").fetchone()
    assert tuple(row) == (None, None)


def test_missing_bundle_fails_open(
    off: tuple[sqlite3.Connection, Any, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    c_off, _, p_off = off
    conn, report, prompts = _run(monkeypatch, on=True, data=tmp_path)  # no models dir
    assert prompts == p_off and len(report.proposals) == 1
    e = _entry(conn)
    assert e is not None and e["status"] == "unavailable" and e["reason"]
    assert {s["status"] for s in e["scores"]} == {"unavailable"}
    (prop,) = _decisions(conn, "proposed")
    assert prop["ml_scores_id"] == e["id"] and "ml" not in prop
    assert _alerts(conn) == [f"ml:score_failed:{FIXTURE_NOW.date().isoformat()}"]
    assert _strip_ml(_decisions(conn, "proposed")) == _strip_ml(_decisions(c_off, "proposed"))


def test_step_error_fails_open(
    off: tuple[sqlite3.Connection, Any, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import arc.pipeline.ml_shadow as mls

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("panel exploded")

    monkeypatch.setattr(mls, "_score", boom)
    conn, report, prompts = _run(monkeypatch, on=True, data=_models(tmp_path))
    assert prompts == off[2] and len(report.proposals) == 1
    assert _entry(conn) is None
    assert all("ml_scores_id" not in d for d in _decisions(conn, "proposed"))
    assert len(_alerts(conn)) == 1


def test_legs_key_is_order_and_spelling_free() -> None:
    a = legs_key([("SPY 261030P00740000", "long"), ("SPY261030P00745000", "short")])
    b = legs_key([("SPY261030P00745000", -1), ("SPY261030P00740000", 1)])
    assert a == b == "+SPY261030P00740000,-SPY261030P00745000"


def test_m2_ranks_within_ticker_sleeve() -> None:
    def s(key: str, ticker: str, sleeve: str | None, e_r: float | None) -> MlScore:
        st = "ok" if e_r is not None else "unavailable"
        return MlScore(key=key, ticker=ticker, kind="bull_put", sleeve=sleeve, status=st, e_r=e_r)

    got = m2_ranks(
        [
            s("a", "SPY", "short", 0.1),
            s("b", "SPY", "short", 0.3),
            s("c", "SPY", "long", -0.1),
            s("d", "QQQ", "short", 0.0),
            s("e", "QQQ", "short", None),
        ]
    )
    assert got == {"b": 1, "a": 2, "c": 1, "d": 1}


def test_card_line_display_only() -> None:
    from arc.approvals.card import _ml_line

    assert _ml_line(None) is None and _ml_line({}) is None
    assert _ml_line({"p_win": 0.6, "e_r": None, "p_stop": 0.2}) is None
    assert (
        _ml_line({"p_win": 0.584, "e_r": 0.04, "p_stop": 0.221})
        == "p_win 0.58 · E[R] +0.04 · p_stop 0.22"
    )
