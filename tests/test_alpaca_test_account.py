"""E6.2c: integration tests use a dedicated paper account, never the production one."""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arc.broker.alpaca_paper import AlpacaPaperBroker, _make_client
from arc.broker.base import TEST_CLIENT_ORDER_PREFIX, MlegLeg, MlegOrder
from arc.data.alpaca import _get_keys
from tests.alpaca_test_account import (
    IntegrationAccountSettings,
    PrefixedOrderBroker,
    integration_keys,
    resolve_test_keys,
    tag_client_order_id,
)

ROOT = Path(__file__).resolve().parents[1]


def acct(**kw: str) -> IntegrationAccountSettings:
    return IntegrationAccountSettings(_env_file=None, **kw)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _no_ambient_alpaca_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic: a shell that sourced ~/.hermes/.env must not leak real keys in."""
    for var in ("ALPACA_API_KEY", "ALPACA_TEST_API_KEY", "ALPACA_TEST_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)


# -- key resolution -----------------------------------------------------------


def test_unset_test_keys_skip() -> None:
    got = resolve_test_keys(acct(ALPACA_API_KEY="PKPROD"))
    assert isinstance(got, str) and got.startswith("skip:")
    got = resolve_test_keys(acct(ALPACA_TEST_API_KEY="PKTEST"))  # secret missing
    assert isinstance(got, str) and got.startswith("skip:")


def test_test_key_equal_to_production_fails() -> None:
    got = resolve_test_keys(
        acct(ALPACA_API_KEY="PKSAME", ALPACA_TEST_API_KEY=" PKSAME ", ALPACA_TEST_SECRET_KEY="s")
    )
    assert isinstance(got, str) and got.startswith("fail:")
    assert "production paper account" in got


def test_distinct_test_keys_resolve() -> None:
    got = resolve_test_keys(
        acct(ALPACA_API_KEY="PKPROD", ALPACA_TEST_API_KEY="PKTEST", ALPACA_TEST_SECRET_KEY="s")
    )
    assert got == ("PKTEST", "s")


def test_integration_keys_skip_and_fail() -> None:
    with pytest.raises(pytest.skip.Exception, match="ALPACA_TEST_API_KEY"):
        integration_keys(acct(ALPACA_API_KEY="PKPROD"))
    same = acct(ALPACA_API_KEY="PKPROD", ALPACA_TEST_API_KEY="PKPROD", ALPACA_TEST_SECRET_KEY="s")
    with pytest.raises(pytest.fail.Exception, match="equals ALPACA_API_KEY"):
        integration_keys(same)
    ok = acct(ALPACA_API_KEY="PKPROD", ALPACA_TEST_API_KEY="PKTEST", ALPACA_TEST_SECRET_KEY="s")
    assert integration_keys(ok) == ("PKTEST", "s")


def test_settings_read_the_environment() -> None:
    env = {"ALPACA_API_KEY": "PKPROD", "ALPACA_TEST_API_KEY": "PKTEST",
           "ALPACA_TEST_SECRET_KEY": "s"}  # fmt: skip
    with patch.dict(os.environ, env, clear=True):
        assert resolve_test_keys(acct()) == ("PKTEST", "s")


# -- adapters accept explicit keys -----------------------------------------------


def test_broker_explicit_keys_win_over_environment() -> None:
    with (
        patch.dict(os.environ, {"ALPACA_API_KEY": "PKPROD", "ALPACA_SECRET_KEY": "sp"}),
        patch("arc.broker.alpaca_paper.TradingClient") as tc,
    ):
        AlpacaPaperBroker(api_key="PKTEST", secret_key="st")
        assert tc.call_args.kwargs["api_key"] == "PKTEST"
        assert tc.call_args.kwargs["secret_key"] == "st"
        assert tc.call_args.kwargs["url_override"] == "https://paper-api.alpaca.markets"
        AlpacaPaperBroker()  # production default unchanged
        assert tc.call_args.kwargs["api_key"] == "PKPROD"


def test_broker_explicit_keys_are_both_or_neither() -> None:
    with pytest.raises(ValueError, match="both"):
        _make_client("PKTEST", None)
    with pytest.raises(RuntimeError, match="must be set"):
        _make_client("", "")


def test_market_data_explicit_keys() -> None:
    with patch.dict(os.environ, {"ALPACA_API_KEY": "PKPROD", "ALPACA_SECRET_KEY": "sp"}):
        assert _get_keys("PKTEST", "st") == ("PKTEST", "st")
        assert _get_keys() == ("PKPROD", "sp")
        with pytest.raises(ValueError, match="both"):
            _get_keys(None, "st")
        with pytest.raises(ValueError, match="non-empty"):
            _get_keys("", "st")
    from arc.data.alpaca import AlpacaMarketData

    with (
        patch("arc.data.alpaca.StockHistoricalDataClient") as sc,
        patch("arc.data.alpaca.OptionHistoricalDataClient"),
        patch("arc.data.alpaca.TradingClient") as tc,
    ):
        AlpacaMarketData(data_feed="iex", options_feed="indicative", api_key="PKT", secret_key="s")
        assert sc.call_args.kwargs["api_key"] == "PKT"
        assert tc.call_args.kwargs["api_key"] == "PKT"


# -- test. prefix -------------------------------------------------------------------


def _order(coid: str | None) -> MlegOrder:
    return MlegOrder(
        legs=[MlegLeg(symbol="SPY261030C00500000", side="buy")],
        limit_price=Decimal("0.01"),
        client_order_id=coid,
    )


def test_wrapper_prefixes_client_order_id_and_delegates() -> None:
    inner = MagicMock()
    inner.submit_mleg.return_value = "brk-1"
    b = PrefixedOrderBroker(inner)
    token = "arc2." + "x" * 100 + ".s3"
    assert b.submit_mleg(_order(token)) == "brk-1"
    sent = inner.submit_mleg.call_args.args[0]
    assert sent.client_order_id == TEST_CLIENT_ORDER_PREFIX + token
    assert len(sent.client_order_id) <= 128
    b.submit_mleg(_order(None))
    assert inner.submit_mleg.call_args.args[0].client_order_id.startswith("test.")
    b.account()
    inner.account.assert_called_once()


def test_tag_is_idempotent_and_bounded() -> None:
    assert tag_client_order_id("test.abc") == "test.abc"
    with pytest.raises(ValueError, match="128"):
        tag_client_order_id("x" * 124)


def test_submit_never_adds_the_test_prefix() -> None:
    """The prefix belongs to the tests' wrapper, not arc.execution.submit()."""
    src = (ROOT / "arc" / "execution" / "submission.py").read_text()
    assert "TEST_CLIENT_ORDER_PREFIX" not in src and '"test.' not in src


def test_production_code_never_reads_test_keys() -> None:
    pat = re.compile(r"ALPACA_TEST_|alpaca_test_", re.IGNORECASE)
    hits = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "arc").rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".yaml", ".yml", ".toml"}
        and pat.search(p.read_text())
    ]
    assert hits == []


def test_integration_workflow_uses_test_secrets() -> None:
    wf = (ROOT / ".github" / "workflows" / "integration.yml").read_text()
    assert "secrets.ALPACA_TEST_API_KEY" in wf and "secrets.ALPACA_TEST_SECRET_KEY" in wf
    assert "secrets.ALPACA_API_KEY" not in wf and "secrets.ALPACA_SECRET_KEY" not in wf
