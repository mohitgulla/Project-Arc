"""D116 (E3.7): sizing, prompt lines and the Tower use the gate's tier helper.

Every consumer must agree with ``check_per_underlying`` for the same book.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal as D

from arc.broker.base import AccountInfo
from arc.config import ArcSettings
from arc.gate import Portfolio, Position, derive
from arc.gate import rules as R
from arc.models import Sizing
from arc.pipeline.portfolio_context import cap_phrase, empty_portfolio_line
from arc.pipeline.steps import portfolio_alloc_tier
from arc.sizing import size_contracts
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.data_overview import _greeks_section
from tests.test_gate import acct, make_proposal

EQ = D("100000")


def cfg(**kw: object) -> ArcSettings:
    base: dict[str, object] = {
        "max_alloc_pct": 0.15,
        "max_alloc_pct_reduced": 0.10,
        "alloc_tier_threshold_pct": 0.60,
    }
    base.update(kw)
    return ArcSettings(_env_file=None, account_profile="margin", **base)  # type: ignore[call-arg]


def _book(other: str, reserved: str = "0") -> Portfolio:
    return Portfolio(
        positions=[Position(underlying="QQQ", max_loss=D(other))],
        reserved_max_loss=D(reserved),
    )


def _size(pf: Portfolio, settings: ArcSettings) -> int:
    tier = portfolio_alloc_tier(pf, EQ, settings)
    return size_contracts(
        suggestion=10**6,
        max_loss_per_contract=D("415"),
        equity=EQ,
        cap_pct=tier.pct,
        cap_label=tier.describe(),
    ).contracts


def test_sizing_and_gate_agree_on_both_tiers() -> None:
    s = cfg()
    for pf, want in ((_book("59000"), 36), (_book("59000", reserved="1000"), 24)):
        n = _size(pf, s)
        assert n == want  # floor(15,000 / 415) = 36; floor(10,000 / 415) = 24
        for k, ok in ((n, True), (n + 1, False)):
            p = make_proposal().model_copy(
                update={"sizing": Sizing(contracts=k, notional=D(415 * k), pct_equity=0.1)}
            )
            assert (R.check_per_underlying(derive(p), acct(), pf, s) == []) is ok


def test_sizing_reason_names_the_tier() -> None:
    tier = portfolio_alloc_tier(_book("60000"), EQ, cfg())
    r = size_contracts(
        suggestion=1,
        max_loss_per_contract=D("20000"),
        equity=EQ,
        cap_pct=tier.pct,
        cap_label=tier.describe(),
    )
    assert r.contracts == 0 and "(10% tier, deployed 60% ≥ 60%)" in r.reason


def test_prompt_lines_name_the_active_tier() -> None:
    info = AccountInfo(account_id="x", equity=EQ, buying_power=EQ, cash=EQ)
    line = empty_portfolio_line(info, cfg())
    assert "15% of equity max loss per underlying (15% tier, deployed 0% < 60%)" in line
    reduced = portfolio_alloc_tier(_book("70000"), EQ, cfg())
    assert cap_phrase(reduced).startswith("10% of equity max loss per underlying (10% tier")


def test_tower_section_uses_the_same_tier(monkeypatch) -> None:
    from types import SimpleNamespace

    import arc.tower.data_overview as ov

    g = SimpleNamespace(equity=100000.0, theta=None)
    monkeypatch.setattr(ov, "_greeks", lambda *a, **k: g)
    monkeypatch.setattr(ov, "_advisory", lambda *a, **k: None)
    monkeypatch.setattr(ov, "_dollar_gamma", lambda *a, **k: None)
    monkeypatch.setattr(ov, "GreeksSection", lambda **kw: SimpleNamespace(**kw))
    rows = [SimpleNamespace(status="open", max_loss=D("50000"), ticker="QQQ")]
    kw = {
        "dollar_delta_cap_pct": 1.0,
        "vega_cap_pct": 0.01,
        "max_alloc_pct": 0.15,
        "stale_after": dt.timedelta(minutes=30),
        "max_alloc_pct_reduced": 0.10,
        "alloc_tier_threshold_pct": 0.60,
    }
    base = _greeks_section(None, rows, **kw)
    assert (base.alloc_tier, base.max_alloc_pct, base.per_underlying_cap) == (
        "base",
        0.15,
        D("15000.00"),
    )
    red = _greeks_section(None, rows, reserved_max_loss=D("10000"), **kw)
    assert (red.alloc_tier, red.max_alloc_pct, red.deployed_pct) == ("reduced", 0.10, 0.6)
    assert red.deployed_max_loss == D("60000")


def test_tower_reserved_reader_tolerates_an_old_store() -> None:
    from arc.tower.data_overview import _reserved
    from tests.test_gate import NOW

    c = connect(":memory:")
    assert _reserved(c, NOW) == 0  # no tables: sqlite3.Error -> 0
    migrate(c)
    assert _reserved(c, NOW) == 0
    assert isinstance(c, sqlite3.Connection)
