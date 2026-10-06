"""Broker adapter registry: venue x env x transport -> adapter (E13.11, D56).

One deterministic Broker layer. Every trading entry point builds its broker
here (via :func:`arc.experiments.broker.trading_broker` or
:func:`resolve_broker`), never by constructing a venue class directly:

======================  ===============================================
``alpaca/paper/rest``   :class:`~arc.broker.alpaca_paper.AlpacaPaperBroker`
                        (default, and the only one that constructs)
``alpaca/live/rest``    :class:`~arc.broker.stubs.AlpacaLiveStub`
``robinhood/live/*``    :class:`~arc.broker.stubs.RobinhoodStub`
``alpaca/*/mcp``        :class:`~arc.broker.stubs.McpStub`
anything else           not registered (e.g. ``robinhood/paper``: no paper mode)
======================  ===============================================

A stub or an unregistered spec raises :class:`BrokerNotAvailable` from
:func:`build_broker` **before any credential read** (stub factories never touch
``os.environ``, files or the network), so no trading code ever holds one.
The registry never widens live access: ``ARC_ENV=live`` keeps the
``~/.arc/live.env`` existence check in :class:`~arc.config.ArcSettings` and
nothing here reads that file. ``arc.execution.submit()`` stays the only path to
``submit_mleg``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings

__all__ = [
    "REGISTRY",
    "BrokerFactory",
    "BrokerInfo",
    "BrokerNotAvailable",
    "BrokerSpec",
    "NotAvailable",
    "build_broker",
    "resolve_broker",
    "spec_from_settings",
]

Venue = Literal["alpaca", "robinhood"]
Env = Literal["paper", "live"]
Transport = Literal["rest", "mcp"]


class BrokerSpec(BaseModel):
    """Which broker a process trades through."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    venue: Venue = "alpaca"
    env: Env = "paper"
    transport: Transport = "rest"
    keys_env: str | None = None  # experiment arm key prefix (ALPACA_EXP); None = default keys

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.venue, self.env, self.transport)

    @property
    def label(self) -> str:
        return "/".join(self.key)


class BrokerInfo(BaseModel):
    """What an adapter is (``adapter.info()``); never carries an account id."""

    model_config = ConfigDict(extra="forbid")

    spec: BrokerSpec
    account_label: str  # "paper" | "exp:XP-3:treatment"
    supports_mleg: bool  # alpaca True; robinhood False (single-leg, D1)
    supports_paper: bool  # robinhood False


class BrokerNotAvailable(RuntimeError):
    """The requested broker is not enabled (only ``alpaca/paper/rest`` is)."""

    def __init__(self, spec: BrokerSpec, reason: str = "") -> None:
        self.spec = spec
        msg = f"{spec.label} not enabled (D1/D56); paper only"
        super().__init__(f"{msg}: {reason}" if reason else msg)


NotAvailable = BrokerNotAvailable  # card alias

#: ``(spec, environ, account_label) -> adapter``. *environ* is only read by the
#: Alpaca paper factory, and only for an arm's ``keys_env`` pair.
BrokerFactory = Callable[[BrokerSpec, "Mapping[str, str] | None", str], "BrokerAdapter"]


def _alpaca_paper(
    spec: BrokerSpec, environ: Mapping[str, str] | None, account_label: str
) -> BrokerAdapter:
    from arc.broker.alpaca_paper import AlpacaPaperBroker

    if spec.keys_env is None:
        return AlpacaPaperBroker(account_label=account_label)
    from arc.experiments.arms import arm_keys

    key, secret = arm_keys(spec.keys_env, environ)
    return AlpacaPaperBroker(api_key=key, secret_key=secret, account_label=account_label)


def _stub(cls_name: str) -> BrokerFactory:
    def make(
        spec: BrokerSpec,
        environ: Mapping[str, str] | None,  # noqa: ARG001 - stubs read nothing
        account_label: str,  # noqa: ARG001
    ) -> BrokerAdapter:
        from arc.broker import stubs

        return getattr(stubs, cls_name)(spec)  # type: ignore[no-any-return]

    return make


REGISTRY: dict[tuple[str, str, str], BrokerFactory] = {
    ("alpaca", "paper", "rest"): _alpaca_paper,
    ("alpaca", "live", "rest"): _stub("AlpacaLiveStub"),
    ("alpaca", "paper", "mcp"): _stub("McpStub"),
    ("alpaca", "live", "mcp"): _stub("McpStub"),
    ("robinhood", "live", "rest"): _stub("RobinhoodStub"),
    ("robinhood", "live", "mcp"): _stub("RobinhoodStub"),
}


def spec_from_settings(settings: ArcSettings, *, keys_env: str | None = None) -> BrokerSpec:
    """``ARC_BROKER_VENUE`` x ``ARC_ENV`` x ``ARC_BROKER_TRANSPORT`` (+ an arm's key prefix)."""
    return BrokerSpec(
        venue=settings.broker_venue,
        env=settings.env.value,  # type: ignore[arg-type]
        transport=settings.broker_transport,
        keys_env=keys_env,
    )


def build_broker(
    spec: BrokerSpec,
    *,
    environ: Mapping[str, str] | None = None,
    account_label: str | None = None,
) -> BrokerAdapter:
    """The adapter for *spec*; :class:`BrokerNotAvailable` unless it is ``alpaca/paper/rest``.

    A registered stub is constructed (it reads nothing) and probed with
    ``info()``, which raises, so the caller never receives a stub.
    """
    factory = REGISTRY.get(spec.key)
    if factory is None:
        raise BrokerNotAvailable(spec, "no adapter registered")
    label = account_label or ("paper" if spec.keys_env is None else f"exp:{spec.keys_env}")
    adapter = factory(spec, environ, label)
    adapter.info()  # type: ignore[attr-defined] - stubs raise BrokerNotAvailable here
    return adapter


def resolve_broker(
    settings: ArcSettings,
    *,
    keys_env: str | None = None,
    environ: Mapping[str, str] | None = None,
    account_label: str | None = None,
) -> BrokerAdapter:
    """The broker *settings* select (default ``alpaca/paper/rest`` on ``ALPACA_API_KEY``)."""
    return build_broker(
        spec_from_settings(settings, keys_env=keys_env),
        environ=environ,
        account_label=account_label,
    )
