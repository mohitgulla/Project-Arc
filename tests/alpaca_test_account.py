"""The dedicated Alpaca *test* paper account for integration tests (E6.2c).

Integration tests must never trade the production paper account: on
2026-09-28/29 their RTH runs left fills with no local record there, the 16:30
reconcile halted production and the next morning's chain was skipped.

- :class:`IntegrationAccountSettings` reads ``ALPACA_TEST_API_KEY`` /
  ``ALPACA_TEST_SECRET_KEY`` (environment first, then ``~/.hermes/.env``). It
  lives under ``tests/`` on purpose: nothing in ``arc/`` reads the TEST_ keys
  (``tests/test_alpaca_test_account.py`` checks that).
- :func:`integration_keys` skips when they are unset and **fails** when the
  test key is the production ``ALPACA_API_KEY`` (the exact accident this card
  exists to stop, so it must not look like a skip).
- :class:`PrefixedOrderBroker` wraps the real adapter and prefixes every
  ``client_order_id`` with ``test.`` just before it reaches the broker, so a
  stray test fill on the production account reconciles as ``fill_test``
  (notice, no halt) instead of ``fill_unknown``. ``arc.execution.submit()``
  is unchanged: it verifies the gate token, then the wrapper tags the id.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from arc.broker.base import TEST_CLIENT_ORDER_PREFIX, MlegOrder

if TYPE_CHECKING:
    from arc.data.alpaca import AlpacaMarketData

__all__ = [
    "IntegrationAccountSettings",
    "PrefixedOrderBroker",
    "integration_broker",
    "integration_keys",
    "integration_market_data",
    "resolve_test_keys",
    "tag_client_order_id",
]

_MAX_COID = 128  # Alpaca's client_order_id limit
RUNBOOK = "docs/OPS.md 5.12 'Two paper accounts'"


class IntegrationAccountSettings(BaseSettings):
    """Keys of the dedicated test paper account (+ the production key, to compare)."""

    model_config = SettingsConfigDict(
        env_file=str(Path.home() / ".hermes" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    test_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("ALPACA_TEST_API_KEY", "alpaca_test_api_key")
    )
    test_secret_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("ALPACA_TEST_SECRET_KEY", "alpaca_test_secret_key")
    )
    production_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("ALPACA_API_KEY", "alpaca_api_key")
    )


def _val(s: SecretStr | None) -> str:
    return s.get_secret_value().strip() if s is not None else ""


def resolve_test_keys(s: IntegrationAccountSettings) -> tuple[str, str] | str:
    """``(key, secret)`` of the test account, or why it can't be used.

    The reason starts with ``skip:`` (keys unset) or ``fail:`` (the test key is
    the production key: running would trade the production paper account).
    """
    key, secret = _val(s.test_api_key), _val(s.test_secret_key)
    if not key or not secret:
        return f"skip: ALPACA_TEST_API_KEY/ALPACA_TEST_SECRET_KEY not set ({RUNBOOK})"
    prod = _val(s.production_api_key)
    if prod and key == prod:
        return (
            "fail: ALPACA_TEST_API_KEY equals ALPACA_API_KEY: integration tests would trade "
            f"the production paper account. Use the dedicated test account ({RUNBOOK})."
        )
    return key, secret


def integration_keys(settings: IntegrationAccountSettings | None = None) -> tuple[str, str]:
    """Test-account keys; ``pytest.skip`` when unset, ``pytest.fail`` when it is production."""
    got = resolve_test_keys(settings if settings is not None else IntegrationAccountSettings())
    if isinstance(got, tuple):
        return got
    kind, _, reason = got.partition(": ")
    if kind == "fail":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def tag_client_order_id(coid: str | None) -> str:
    """``test.<coid>`` (idempotent); a fresh ``test.<hex>`` when there is none."""
    if coid is None:  # the adapter would otherwise mint an untagged ``arc-…`` id
        return f"{TEST_CLIENT_ORDER_PREFIX}{uuid.uuid4().hex[:12]}"
    if coid.startswith(TEST_CLIENT_ORDER_PREFIX):
        return coid
    tagged = TEST_CLIENT_ORDER_PREFIX + coid
    if len(tagged) > _MAX_COID:
        msg = f"test client_order_id is {len(tagged)} chars (> {_MAX_COID})"
        raise ValueError(msg)
    return tagged


class PrefixedOrderBroker:
    """Broker wrapper for integration tests: every order's client id gets ``test.``.

    Everything else is delegated unchanged to the wrapped adapter.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def submit_mleg(self, order: MlegOrder) -> str:
        tagged = order.model_copy(
            update={"client_order_id": tag_client_order_id(order.client_order_id)}
        )
        return self._inner.submit_mleg(tagged)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def integration_broker() -> PrefixedOrderBroker:
    """The test account's paper broker, with ``test.``-tagged orders."""
    from arc.broker.alpaca_paper import AlpacaPaperBroker

    key, secret = integration_keys()
    return PrefixedOrderBroker(AlpacaPaperBroker(api_key=key, secret_key=secret))


def integration_market_data() -> AlpacaMarketData:
    """Market data authenticated as the test account."""
    from arc.data.alpaca import AlpacaMarketData

    key, secret = integration_keys()
    return AlpacaMarketData(api_key=key, secret_key=secret)
