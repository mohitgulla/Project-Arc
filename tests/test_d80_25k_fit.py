"""E19.2 (D80): the $25K fit-check script replays sizing + gate on a read-only store."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from arc.account_profiles import DayTradeRule, load_account_profiles
from arc.config import ArcSettings
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.structures.builders import credit_vertical, debit_vertical

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


fit = _load("d80_25k_fit", REPO / "scripts" / "d80_25k_fit.py")

EXP = dt.date(2026, 11, 20)
DAY = dt.date(2026, 10, 9)
EQ = Decimal(25000)


def _settings() -> ArcSettings:
    return ArcSettings(max_alloc_pct=0.075)


def _cheap() -> Any:  # $4.00 debit on a $10-wide bull call: $400 max loss / lot
    return debit_vertical(
        "c", "XOM", EXP, long_strike=170, long_premium=6.0, short_strike=180, short_premium=2.0
    )


def _dear() -> Any:  # $25 debit on a $50-wide bull call: $2,500 / lot > the $1,875 cap
    return debit_vertical(
        "c", "MU", EXP, long_strike=1000, long_premium=60, short_strike=1050, short_premium=35
    )


def _replay(st: Any, ticker: str, suggestion: int, **kw: Any) -> Any:
    return fit.replay(
        st,
        source="open",
        ticker=ticker,
        day=DAY,
        suggestion=suggestion,
        lots_orig=suggestion,
        band=kw.pop("band", None),
        settings=_settings(),
        equity=EQ,
        spot=kw.pop("spot", 170.0),
        beta=Decimal(1),
        **kw,
    )


def test_cheap_structure_fits_and_keeps_its_lots() -> None:
    row = _replay(_cheap(), "XOM", 2)
    assert row.fits
    assert row.lots_25k == 2
    assert row.cap_lots == 4  # floor(1875 / 400)
    assert row.blocks == []


def test_dear_structure_is_cap_zero() -> None:
    row = _replay(_dear(), "MU", 1, spot=1020.0)
    assert not row.fits
    assert row.lots_25k == 0
    assert row.blocks[0] == "sizing:cap_zero"


def test_band_worst_price_adds_to_max_loss() -> None:
    band = fit.PriceBand(lo=Decimal("4.00"), hi=Decimal("4.20"), max_steps=3)
    row = _replay(_cheap(), "XOM", 1, band=band)
    assert row.worst_loss_unit == pytest.approx(420.0)


def test_menu_shrinks_to_a_passing_lot_count() -> None:
    # $Δ cap = 1.0 × 25,000. 4 lots × ~40 Δ × $170 ≈ $27k fails; fewer lots pass.
    st = _cheap().model_copy(update={"greeks": _cheap().greeks.model_copy(update={"delta": 40.0})})
    row = fit.replay(
        st,
        source="menu",
        ticker="XOM",
        day=DAY,
        suggestion=10**6,
        lots_orig=None,
        band=None,
        settings=_settings(),
        equity=EQ,
        spot=170.0,
        beta=Decimal(1),
        shrink=True,
    )
    assert row.fits
    assert row.lots_25k == 3  # 3 × 40 × 170 = 20,400 ≤ 25,000


def test_menu_structure_skips_credit_items() -> None:
    cr = credit_vertical(
        "p", "XOM", EXP, short_strike=160, short_premium=3.0, long_strike=150, long_premium=1.0
    )
    item = {
        "ticker": "XOM",
        "net_debit_credit": float(cr.net_debit_credit),
        "max_loss": float(cr.max_loss or 0),
        "legs": [{"occ_symbol": x.occ_symbol, "side": x.side.value} for x in cr.legs],
    }
    assert fit.menu_structure(item) is None


def test_bsm_target_vertical_scales_with_price_and_vol() -> None:
    s = _settings()
    a = fit.bsm_target_vertical(100.0, 0.30, s)
    assert 0 < a < 100 * 100
    assert fit.bsm_target_vertical(200.0, 0.30, s) == pytest.approx(2 * a, rel=1e-3)
    assert fit.bsm_target_vertical(100.0, 0.60, s) > a


def test_fees_are_a_rounding_error_on_600_debit() -> None:
    f = fit.fee_example(fit.load_cost_model(), 600.0, 2)
    assert f["total"] < 0.5
    assert f["pct"] < 0.1


def test_no_profile_enables_pdt() -> None:
    profs = load_account_profiles()
    assert all(
        p.day_trades.rule is not DayTradeRule.PATTERN_DAY_TRADER for p in profs.profiles.values()
    )


# ---------------------------------------------------------------------------
# End to end on a tiny store, opened read-only
# ---------------------------------------------------------------------------


def _store(path: Path) -> None:
    conn = connect(path)
    migrate(conn)
    at = "2026-10-09T14:00:00Z"
    conn.execute(
        "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at) "
        "VALUES ('c1', 'XOM', 'bullish', 'sector', 0.6, ?)",
        (at,),
    )
    for i, (t, st, lots, spot) in enumerate(
        [("XOM", _cheap(), 2, "170"), ("MU", _dear(), 1, "1020")]
    ):
        h = f"h{i}"
        conn.execute(
            "INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis, "
            "quant_json, sizing_json, expires_at, created_at, ticker, kind, spot) "
            "VALUES (?, 'c1', ?, ?, '', '{}', ?, ?, ?, ?, 'open', ?)",
            (
                f"p{i}",
                h,
                st.model_dump_json(),
                json.dumps({"contracts": lots, "notional": "0", "pct_equity": 0.01}),
                at,
                at,
                t,
                spot,
            ),
        )
        conn.execute(
            "INSERT INTO open_structures (id, ticker, open_proposal_hash, candidate_id, "
            "structure_json, contracts, entry_net, opened_at, status, closed_at) "
            "VALUES (?, ?, ?, 'c1', ?, ?, ?, ?, ?, ?)",
            (
                f"o{i}",
                t,
                h,
                st.model_dump_json(),
                lots,
                str(st.net_debit_credit),
                at,
                "closed" if i else "open",
                "2026-10-09T18:00:00Z" if i else None,
            ),
        )
    menu = {
        "structures": [
            {
                "ticker": "XOM",
                "net_debit_credit": 4.0,
                "max_loss": 400.0,
                "max_gain": 600.0,
                "dte": 42,
                "greeks": {"delta": 30.0, "vega": 10.0},
                "legs": [
                    {"occ_symbol": x.occ_symbol, "side": x.side.value, "ratio": 1}
                    for x in _cheap().legs
                ],
            }
        ]
    }
    active = {
        "as_of": "2026-10-09",
        "members": [{"ticker": "XOM", "tier": "core"}, {"ticker": "SNDK", "tier": "momentum"}],
    }
    for kind, payload in (("structures", menu), ("active_universe", active)):
        conn.execute(
            "INSERT INTO context_entries (id, kind, subject, payload, schema_version, "
            "produced_by, created_at, valid_from) VALUES (?, ?, 'session', ?, 1, 't', ?, ?)",
            (kind, kind, json.dumps(payload), at, at),
        )
    conn.execute(
        "INSERT INTO iv_daily (ticker, day, iv30, method, source, spot, created_at) "
        "VALUES ('SNDK', '2026-10-09', 0.64, 'chain_cm30', 'alpaca_cm30', 1580, ?)",
        (at,),
    )
    conn.commit()
    conn.close()


def test_end_to_end_report_reads_the_store_read_only(tmp_path: Path) -> None:
    db = tmp_path / "arc.db"
    _store(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    out = tmp_path / "r.md"
    js = tmp_path / "r.json"
    assert fit.main(["--db", str(db), "--out", str(out), "--json", str(js)]) == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    md = out.read_text()
    assert "**1 fit**, 1 don't" in md  # XOM fits, MU is cap_zero
    assert "per-underlying cap **$1,250**" in md  # shipped 5% default, no overrides
    assert "| 2026-10-09 | MU | vertical_debit |" in md
    assert "sizing:cap_zero" in md
    assert "Old book: peak **2**" in md
    assert "## 5. PDT is moot" in md
    rows = json.loads(js.read_text())["rows"]
    assert {r["source"] for r in rows} == {"open", "menu"}
    # SNDK has no chain: estimated from IV30 (no calibration names -> ratio 1).
    uni = {u["ticker"]: u for u in json.loads(js.read_text())["universe"]}
    assert uni["SNDK"]["source"].startswith("estimate")
    assert uni["XOM"]["fits"] == "yes"


def test_missing_store_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fit.main(["--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "r.md")])
    assert not (tmp_path / "nope.db").exists()
