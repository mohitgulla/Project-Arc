"""E13.11 (D56): broker adapter registry, fail-closed stubs, single-leg venue refusal."""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import os
import socket
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from arc.broker import registry as R
from arc.broker.alpaca_paper import AlpacaPaperBroker
from arc.broker.registry import (
    REGISTRY,
    BrokerInfo,
    BrokerNotAvailable,
    BrokerSpec,
    build_broker,
    resolve_broker,
    spec_from_settings,
)
from arc.broker.stubs import AlpacaLiveStub, McpStub, RobinhoodStub, StubBroker
from arc.config import ArcEnv, ArcSettings
from arc.control.registry import NEVER_TUNABLE

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 5, 16, 30, tzinfo=dt.timezone(dt.timedelta(hours=-4)))


def settings(**kw: object) -> ArcSettings:
    return ArcSettings(_env_file=None, **kw)  # type: ignore[arg-type, call-arg]


# ---------------------------------------------------------------------------
# Sentinels: a stub must not read the environment, a file or the network
# ---------------------------------------------------------------------------


class _EnvAccessed(AssertionError):
    pass


class _SentinelEnviron(dict):  # type: ignore[type-arg]
    """``os.environ`` stand-in: any read fails the test."""

    def _boom(self, *a: object, **k: object) -> object:
        raise _EnvAccessed(f"environment read: {a!r}")

    __getitem__ = get = __contains__ = keys = items = values = __iter__ = _boom  # type: ignore[assignment]
    copy = setdefault = pop = _boom  # type: ignore[assignment]


@contextlib.contextmanager
def no_io() -> Iterator[None]:
    """Fail on any env read, file open or socket inside the block (restored on exit)."""

    def no_open(*a: object, **k: object) -> object:
        raise AssertionError(f"file opened: {a!r}")

    def no_socket(*a: object, **k: object) -> object:
        raise AssertionError("network used")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "environ", _SentinelEnviron())
        mp.setattr(os, "getenv", lambda *a, **k: _SentinelEnviron()._boom(*a))
        mp.setattr("builtins.open", no_open)
        mp.setattr(socket, "socket", no_socket)
        mp.setattr(socket, "create_connection", no_socket)
        yield


STUB_SPECS = [
    (BrokerSpec(venue="alpaca", env="live"), AlpacaLiveStub),
    (BrokerSpec(venue="alpaca", env="paper", transport="mcp"), McpStub),
    (BrokerSpec(venue="alpaca", env="live", transport="mcp"), McpStub),
    (BrokerSpec(venue="robinhood", env="live"), RobinhoodStub),
    (BrokerSpec(venue="robinhood", env="live", transport="mcp"), RobinhoodStub),
]


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_keys_and_only_paper_is_real() -> None:
    assert set(REGISTRY) == {
        ("alpaca", "paper", "rest"),
        ("alpaca", "live", "rest"),
        ("alpaca", "paper", "mcp"),
        ("alpaca", "live", "mcp"),
        ("robinhood", "live", "rest"),
        ("robinhood", "live", "mcp"),
    }
    assert {s.key for s, _ in STUB_SPECS} == set(REGISTRY) - {("alpaca", "paper", "rest")}


def test_spec_is_frozen_and_strict() -> None:
    s = BrokerSpec()
    assert (s.venue, s.env, s.transport, s.keys_env) == ("alpaca", "paper", "rest", None)
    assert s.label == "alpaca/paper/rest"
    with pytest.raises(ValueError, match="frozen"):
        s.venue = "robinhood"  # type: ignore[misc]
    with pytest.raises(ValueError, match="extra"):
        BrokerSpec(account="123")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        BrokerSpec(venue="ibkr")  # type: ignore[arg-type]


def test_not_available_alias_and_message() -> None:
    assert R.NotAvailable is BrokerNotAvailable
    exc = BrokerNotAvailable(BrokerSpec(venue="robinhood", env="live"))
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "robinhood/live/rest not enabled (D1/D56); paper only"
    assert exc.spec.venue == "robinhood"
    assert "why" in str(BrokerNotAvailable(BrokerSpec(), "why"))


# ---------------------------------------------------------------------------
# Default: alpaca/paper/rest
# ---------------------------------------------------------------------------


def test_default_settings_resolve_alpaca_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, object]] = []

    def fake_init(self: AlpacaPaperBroker, client: object = None, **kw: object) -> None:
        built.append(kw)
        self._client = MagicMock()
        self._account_label = str(kw.get("account_label", "paper"))

    monkeypatch.setattr(AlpacaPaperBroker, "__init__", fake_init)
    s = settings()
    assert (s.broker_venue, s.env, s.broker_transport) == ("alpaca", ArcEnv.PAPER, "rest")
    assert spec_from_settings(s) == BrokerSpec()
    b = resolve_broker(s)
    assert isinstance(b, AlpacaPaperBroker)
    assert built == [{"account_label": "paper"}]  # production default keys
    info = b.info()
    assert info == BrokerInfo(
        spec=BrokerSpec(), account_label="paper", supports_mleg=True, supports_paper=True
    )
    assert (b.venue, b.env, b.supports_mleg) == ("alpaca", "paper", True)


def test_arm_keys_env_passes_the_arm_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[dict[str, object]] = []

    def fake_init(self: AlpacaPaperBroker, client: object = None, **kw: object) -> None:
        built.append(kw)
        self._client = MagicMock()
        self._account_label = str(kw["account_label"])

    monkeypatch.setattr(AlpacaPaperBroker, "__init__", fake_init)
    env = {"ALPACA_EXP_API_KEY": "exp", "ALPACA_EXP_SECRET_KEY": "exp-s", "ALPACA_API_KEY": "p"}
    b = resolve_broker(settings(), keys_env="ALPACA_EXP", environ=env, account_label="exp:XP-3:a")
    assert built == [{"api_key": "exp", "secret_key": "exp-s", "account_label": "exp:XP-3:a"}]
    assert b.info().account_label == "exp:XP-3:a"  # type: ignore[attr-defined]
    # default label when none given
    build_broker(BrokerSpec(keys_env="ALPACA_EXP"), environ=env)
    assert built[-1]["account_label"] == "exp:ALPACA_EXP"


def test_arm_keys_env_never_production_or_test() -> None:
    from arc.experiments.arms import ArmKeyError

    for prefix in ("ALPACA", "ALPACA_TEST"):
        with pytest.raises(ArmKeyError):
            resolve_broker(settings(), keys_env=prefix, environ={})


def test_real_paper_adapter_info_without_network() -> None:
    b = AlpacaPaperBroker(client=MagicMock())
    assert b.info().spec.label == "alpaca/paper/rest"
    assert b.info().account_label == "paper"


# ---------------------------------------------------------------------------
# Stubs: refuse, read nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("spec", "cls"), STUB_SPECS, ids=lambda x: getattr(x, "label", ""))
def test_build_broker_refuses_every_stub_without_io(
    spec: BrokerSpec, cls: type[StubBroker]
) -> None:
    with pytest.raises(BrokerNotAvailable) as e, no_io():
        build_broker(spec, environ=None)
    assert e.value.spec == spec
    assert f"{spec.label} not enabled (D1/D56); paper only" in str(e.value)


@pytest.mark.parametrize(("spec", "cls"), STUB_SPECS, ids=lambda x: getattr(x, "label", ""))
def test_every_stub_method_raises_without_io(spec: BrokerSpec, cls: type[StubBroker]) -> None:
    with no_io():
        stub = cls(spec)
    assert (stub.spec, stub.venue, stub.env) == (spec, spec.venue, spec.env)
    calls = [
        stub.info,
        stub.account,
        stub.positions,
        lambda: stub.submit_mleg(MagicMock()),
        lambda: stub.cancel("x"),
        lambda: stub.order_status("x"),
        lambda: stub.fills(NOW),
        lambda: stub.option_orders_since(NOW),
    ]
    for call in calls:
        with pytest.raises(BrokerNotAvailable), no_io():
            call()


def test_stub_construction_logs_warning() -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        RobinhoodStub(BrokerSpec(venue="robinhood", env="live"))
    (entry,) = [x for x in logs if x["event"] == "broker.stub_constructed"]
    assert entry["log_level"] == "warning"
    assert entry["broker"] == "robinhood/live/rest"


def test_robinhood_stub_declares_d1_constraints() -> None:
    assert RobinhoodStub.supports_mleg is False
    assert RobinhoodStub.supports_paper is False
    assert AlpacaLiveStub.supports_mleg is True


@pytest.mark.parametrize(
    "spec",
    [
        BrokerSpec(venue="robinhood", env="paper"),  # Robinhood has no paper mode (D1)
        BrokerSpec(venue="robinhood", env="paper", transport="mcp"),
    ],
    ids=lambda s: s.label,
)
def test_unregistered_spec_refused(spec: BrokerSpec) -> None:
    with pytest.raises(BrokerNotAvailable, match="no adapter registered"), no_io():
        build_broker(spec)


# ---------------------------------------------------------------------------
# Settings: ARC_BROKER_VENUE / ARC_BROKER_TRANSPORT
# ---------------------------------------------------------------------------


def test_settings_env_vars_select_the_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_BROKER_VENUE", "robinhood")
    s = settings()
    assert s.broker_venue == "robinhood"
    # robinhood + paper is not even registered (no paper mode)
    with pytest.raises(BrokerNotAvailable, match="robinhood/paper/rest"):
        resolve_broker(s)
    monkeypatch.setenv("ARC_BROKER_VENUE", "alpaca")
    monkeypatch.setenv("ARC_BROKER_TRANSPORT", "mcp")
    with pytest.raises(BrokerNotAvailable, match="alpaca/paper/mcp"):
        resolve_broker(settings())


def test_settings_reject_unknown_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_BROKER_VENUE", "ibkr")
    with pytest.raises(ValueError, match="broker_venue"):
        settings()


def test_live_still_needs_live_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import arc.config as C

    monkeypatch.setattr(C, "_LIVE_ENV_PATH", tmp_path / "live.env")  # does not exist
    with pytest.raises(ValueError, match="live.env"):
        settings(env="live")


def test_live_env_resolves_only_to_the_stub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import arc.config as C

    live_env = tmp_path / "live.env"
    live_env.write_text("")  # existence only; nothing reads it
    monkeypatch.setattr(C, "_LIVE_ENV_PATH", live_env)
    s = settings(env="live")
    with pytest.raises(BrokerNotAvailable, match="alpaca/live/rest"):
        resolve_broker(s)


def test_venue_and_transport_are_never_tunable() -> None:
    assert {"broker_venue", "broker_transport"} <= NEVER_TUNABLE
    from arc.control.registry import TunableError, lookup

    for key in ("broker_venue", "broker_transport"):
        with pytest.raises(TunableError):
            lookup(key)


# ---------------------------------------------------------------------------
# Call sites go through the registry
# ---------------------------------------------------------------------------


def _calls_to(name: str) -> list[str]:
    hits: list[str] = []
    for path in sorted((REPO / "arc").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if called == name:
                    hits.append(str(path.relative_to(REPO)))
    return hits


def test_only_the_registry_constructs_alpaca_paper_broker() -> None:
    assert sorted(set(_calls_to("AlpacaPaperBroker"))) == ["arc/broker/registry.py"]


def test_only_submission_calls_submit_mleg() -> None:
    assert sorted(set(_calls_to("submit_mleg"))) == ["arc/execution/submission.py"]


def test_trading_broker_control_store_uses_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.experiments import broker as XB
    from arc.store.db import connect
    from arc.store.migrate import migrate

    seen: list[tuple[str, str | None]] = []
    sentinel = object()

    def fake_resolve(s: ArcSettings, *, keys_env: str | None = None, **kw: object) -> object:
        seen.append((s.broker_venue, keys_env))
        return sentinel

    monkeypatch.setattr(R, "resolve_broker", fake_resolve)
    conn = connect(":memory:")
    migrate(conn)
    assert XB.trading_broker(conn, settings()) is sentinel
    assert seen == [("alpaca", None)]
    monkeypatch.undo()  # the real registry: robinhood/paper is refused, no network
    monkeypatch.setenv("ARC_BROKER_VENUE", "robinhood")
    with pytest.raises(BrokerNotAvailable):
        XB.trading_broker(conn, settings())


def test_trading_broker_arm_store_passes_keys_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.experiments import broker as XB
    from arc.experiments.arms import ArmIdentity, write_identity
    from arc.experiments.virtual import open_account
    from arc.store.db import connect
    from arc.store.migrate import migrate

    seen: list[dict[str, object]] = []

    def fake_resolve(s: ArcSettings, **kw: object) -> object:
        seen.append(kw)
        inner = MagicMock()
        inner.account.return_value = MagicMock(equity=D(1))
        return inner

    monkeypatch.setattr(R, "resolve_broker", fake_resolve)
    conn = connect(":memory:")
    migrate(conn)
    now = dt.datetime(2026, 10, 5, 10, 0, tzinfo=NOW.tzinfo)
    write_identity(
        conn,
        ArmIdentity(
            arm_id="XP-3:treatment", experiment_id="XP-3", arm="treatment",
            spec_arm="treatment", keys_env="ALPACA_EXP", control_db="/nonexistent.db",
            overlay={}, created_at=now,
        ),
    )  # fmt: skip
    open_account(conn, "XP-3:treatment", t0_equity=D(10000), legacy={}, at=now)
    env = {"X": "y"}
    XB.trading_broker(conn, settings(account_profile="cash_debit"), environ=env, now=lambda: now)
    assert seen == [
        {"keys_env": "ALPACA_EXP", "environ": env, "account_label": "exp:XP-3:treatment"}
    ]


def test_pipeline_env_live_resolves_broker_via_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PipelineEnv.live`` builds its broker through the registry; personas get none."""
    from arc.pipeline.env import PipelineEnv

    sentinel = MagicMock()
    monkeypatch.setattr(R, "resolve_broker", lambda s, **kw: sentinel)
    monkeypatch.setattr("arc.data.alpaca.AlpacaMarketData", MagicMock())
    env = PipelineEnv.live(settings(), broker=True)
    assert env.broker is sentinel
    for llm in env.llms.values():
        assert not any(v is sentinel for v in vars(llm).values())
    monkeypatch.setattr(
        R, "resolve_broker", lambda s, **kw: (_ for _ in ()).throw(BrokerNotAvailable(BrokerSpec()))
    )
    with pytest.raises(BrokerNotAvailable):
        PipelineEnv.live(settings(), broker=True)


# ---------------------------------------------------------------------------
# CLIs: a non-paper venue is refused before any credential read
# ---------------------------------------------------------------------------


def _json_out(text: str) -> dict[str, object]:
    """The CLI's JSON document (structlog lines share stdout)."""
    import json

    start = text.index("\n{") + 1 if not text.startswith("{") else 0
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj  # type: ignore[no-any-return]


def _reconcile_args(db: str, now: str | None = None) -> object:
    import argparse

    return argparse.Namespace(db=db, no_halt=True, no_settle=True, now=now)


def test_reconcile_cli_refuses_robinhood(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.reconcile.cli import run_reconcile

    monkeypatch.setenv("ARC_BROKER_VENUE", "robinhood")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    rc = run_reconcile(_reconcile_args(str(tmp_path / "r.db")))  # type: ignore[arg-type]
    out = _json_out(capsys.readouterr().out)
    assert rc == 2
    assert out == {
        "status": "refused",
        "detail": "robinhood/paper/rest not enabled (D1/D56); paper only: no adapter registered",
        "broker": "robinhood/paper/rest",
    }


def test_reconcile_cli_now_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.reconcile.cli import run_reconcile
    from tests.test_reconcile import FakeBroker

    db = str(tmp_path / "r.db")
    rc = run_reconcile(_reconcile_args(db, "2026-10-05T16:30-04:00"), broker=FakeBroker())  # type: ignore[arg-type]
    out = _json_out(capsys.readouterr().out)
    assert rc == 0 and out["day"] == "2026-10-05"
    assert run_reconcile(_reconcile_args(db, "2026-10-05T16:30"), broker=FakeBroker()) == 2  # type: ignore[arg-type]


def test_execute_cli_refuses_robinhood(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``arc execute`` reaches the broker build only past its own checks; build it directly."""
    from arc.experiments.broker import trading_broker
    from arc.store.db import connect
    from arc.store.migrate import migrate

    monkeypatch.setenv("ARC_BROKER_VENUE", "robinhood")
    conn = connect(str(tmp_path / "e.db"))
    migrate(conn)
    with pytest.raises(BrokerNotAvailable, match="robinhood"):
        trading_broker(conn)
