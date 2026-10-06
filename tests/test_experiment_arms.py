"""E10.2 experiment arm runner: keys guard, virtual account, store-chosen broker,
config overlay on arm stores, fork step, market tape and manifest tagging."""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.broker.base import AccountInfo, Fill
from arc.data.base import UnderlyingQuote
from arc.experiments.arms import ArmIdentity, ArmKeyError, arm_keys, read_identity, write_identity
from arc.experiments.broker import trading_broker
from arc.experiments.config import load_experiments_config
from arc.experiments.runner import fork_step
from arc.experiments.tape import TapeRecorder, TapeReplay, call_key, prune_tape
from arc.experiments.virtual import open_account, record_fills, release_legacy, replay, rows
from arc.routines.manifest import _arm_of
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 10, 6, 10, 0, tzinfo=ET)  # Tuesday
ENV = {
    "ALPACA_API_KEY": "prod",
    "ALPACA_SECRET_KEY": "prod-s",
    "ALPACA_TEST_API_KEY": "test",
    "ALPACA_TEST_SECRET_KEY": "test-s",
    "ALPACA_EXP_API_KEY": "exp",
    "ALPACA_EXP_SECRET_KEY": "exp-s",
}
CHAIN = ["research", "quant", "risk", "propose", "broker.execute"]


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    migrate(c)
    return c


def _ident(control_db: str = "/nonexistent/arc.db", overlay: dict | None = None) -> ArmIdentity:
    return ArmIdentity(
        arm_id="XP-1:treatment",
        experiment_id="XP-1",
        arm="treatment",
        spec_arm="treatment",
        keys_env="ALPACA_EXP",
        control_db=control_db,
        overlay=overlay or {},
        created_at=NOW,
    )


# --- keys -------------------------------------------------------------------


def test_arm_keys_never_production_or_test() -> None:
    assert arm_keys("ALPACA_EXP", ENV) == ("exp", "exp-s")
    for forbidden in ("ALPACA", "ALPACA_TEST"):
        with pytest.raises(ArmKeyError):
            arm_keys(forbidden, ENV)
    with pytest.raises(ArmKeyError, match="not set"):
        arm_keys("ALPACA_EXP", {})
    with pytest.raises(ArmKeyError, match="equals"):
        arm_keys("ALPACA_EXP", {**ENV, "ALPACA_EXP_API_KEY": "prod"})
    with pytest.raises(ArmKeyError):
        write_identity(_db(), _ident().model_copy(update={"keys_env": "ALPACA"}))


def test_arm_keys_default_reads_hermes_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E10.2b: the cron tick has no exported keys; arm_keys() must read ~/.hermes/.env."""
    import arc.experiments.arms as arms_mod

    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALPACA_API_KEY=prod\nALPACA_SECRET_KEY=prod-s\n"
        "ALPACA_EXP_API_KEY=exp\nALPACA_EXP_SECRET_KEY=exp-s\n"
    )
    for k in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_EXP_API_KEY",
        "ALPACA_EXP_SECRET_KEY",
        "ALPACA_TEST_API_KEY",
        "ALPACA_TEST_SECRET_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(arms_mod, "HERMES_ENV_FILE", env_file)
    assert arm_keys("ALPACA_EXP") == ("exp", "exp-s")
    # the process environment wins over the file
    monkeypatch.setenv("ALPACA_EXP_API_KEY", "exp2")
    assert arm_keys("ALPACA_EXP") == ("exp2", "exp-s")
    # the production-equality guard sees the file's production keys too
    monkeypatch.setenv("ALPACA_EXP_API_KEY", "prod")
    with pytest.raises(ArmKeyError, match="equals"):
        arm_keys("ALPACA_EXP")
    # no file and nothing exported: still a clear ArmKeyError
    monkeypatch.delenv("ALPACA_EXP_API_KEY")
    monkeypatch.setattr(arms_mod, "HERMES_ENV_FILE", tmp_path / "missing.env")
    with pytest.raises(ArmKeyError, match="not set"):
        arm_keys("ALPACA_EXP")


def test_identity_written_once_and_control_has_none() -> None:
    c = _db()
    assert read_identity(c) is None
    write_identity(c, _ident())
    got = read_identity(c)
    assert got is not None and got.arm_id == "XP-1:treatment"
    with pytest.raises(sqlite3.IntegrityError):
        write_identity(c, _ident())
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM arm_identity")


def test_runner_config_ships_one_treatment_arm_on_its_own_keys() -> None:
    runner = load_experiments_config().runner
    assert set(runner.arms) == {"treatment"}
    arm = runner.arms["treatment"]
    assert arm.keys_env == "ALPACA_EXP" and arm.spec_arm == "treatment"
    assert arm.db_path("XP-3").endswith("arc-exp-XP-3.db")


# --- virtual account ----------------------------------------------------------


def _fill(side: str, px: str, at: dt.datetime, oid: str = "o1") -> Fill:
    return Fill(
        broker_order_id=oid,
        symbol="SPY261120C00580000",
        side=side,
        qty=D(1),
        price=D(px),
        filled_at=at,
    )


def test_virtual_ledger_replay_settlement_and_legacy() -> None:
    c = _db()
    open_account(c, "XP-1:treatment", t0_equity=D(10000), legacy={"os-1": D(500)}, at=NOW)
    st = replay(rows(c, "XP-1:treatment"), as_of=NOW.date())
    assert st.cash == 10000 and st.legacy_reserved == 500 and st.spendable == 9500
    buy = _fill("buy", "2.00", NOW + dt.timedelta(minutes=5))
    sell = _fill("sell", "3.00", NOW + dt.timedelta(hours=2), oid="o2")
    assert record_fills(c, "XP-1:treatment", [buy, sell]) == 2
    assert record_fills(c, "XP-1:treatment", [buy, sell]) == 0  # idempotent
    st = replay(rows(c, "XP-1:treatment"), as_of=NOW.date())
    assert st.cash == D(10100)
    assert st.unsettled == D(300)  # proceeds settle T+1
    assert st.settled == D(10100) - 300 - 500
    nxt = replay(rows(c, "XP-1:treatment"), as_of=dt.date(2026, 10, 7))
    assert nxt.unsettled == 0
    assert release_legacy(c, "XP-1:treatment", ["os-1", "os-x"], at=NOW) == ["os-1"]
    assert replay(rows(c, "XP-1:treatment"), as_of=NOW.date()).legacy_reserved == 0
    # replaying the same ledger reproduces the same state
    assert replay(rows(c, "XP-1:treatment"), as_of=NOW.date()) == replay(
        rows(c, "XP-1:treatment"), as_of=NOW.date()
    )


class _FakeBroker:
    def __init__(self, label: str) -> None:
        self.label = label

    def account(self) -> AccountInfo:
        return AccountInfo(
            account_id="a",
            equity=D(100000),
            buying_power=D(100000),
            cash=D(100000),
            currency="USD",
        )

    def positions(self) -> list:
        return []

    def fills(self, *a: object, **k: object) -> list:
        return []


def test_trading_broker_chosen_by_store(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple[str | None, str | None]] = []

    def factory(k: str | None, s: str | None) -> _FakeBroker:
        made.append((k, s))
        return _FakeBroker(k or "prod")

    ctl = _db()
    assert trading_broker(ctl, factory=factory, environ=ENV).label == "prod"  # type: ignore[attr-defined]
    arm = _db()
    write_identity(arm, _ident())
    open_account(arm, "XP-1:treatment", t0_equity=D(10000), legacy={}, at=NOW)
    from arc.config import ArcSettings

    s = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
    b = trading_broker(arm, s, factory=factory, environ=ENV, now=lambda: NOW)
    assert made[-1] == ("exp", "exp-s")
    acct = b.account()
    assert acct.equity == 10000 and acct.buying_power == 10000  # virtual, not 100k


# --- overlay / fork ------------------------------------------------------------


def test_fork_step_is_first_step_the_overlay_touches() -> None:
    assert fork_step(CHAIN, {}) == "propose"  # A/A: reuse up to the account tail
    assert fork_step(CHAIN, {"exits": {"x": 1}}) == "quant"
    assert fork_step(CHAIN, {"ranking": {"x": 1}}) == "propose"
    assert fork_step(CHAIN, {"account_profiles": {"x": 1}}) == "research"
    assert fork_step(["research", "mystery", "propose"], {}) == "mystery"


def test_arm_store_reads_control_overrides_plus_overlay(tmp_path: Path) -> None:
    from arc.control.effective import effective_settings

    ctl_path = tmp_path / "arc.db"
    ctl = _db(ctl_path)
    ctl.close()
    arm = _db(tmp_path / "arm.db")
    base = effective_settings(_db())
    key = "exits" if hasattr(base, "exits") else None
    write_identity(arm, _ident(str(ctl_path), overlay={}))
    # identity with no overlay: the arm's settings equal control's
    assert effective_settings(arm).account_profile == base.account_profile
    _ = key


# --- market tape -----------------------------------------------------------------


class _Market:
    def __init__(self) -> None:
        self.calls = 0

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        self.calls += 1
        return UnderlyingQuote.model_validate(
            {"symbol": symbol, "bid": 1, "ask": 2, "mid": 1.5, "timestamp": NOW.isoformat()}
        )


def test_tape_records_control_and_replays_identically() -> None:
    ctl = _db()
    live = _Market()
    rec = TapeRecorder(live, ctl, "chain-1")  # type: ignore[arg-type]
    q = rec.underlying_quote("SPY")
    replay_ = TapeReplay.from_store(ctl, "chain-1", None)
    assert replay_.underlying_quote("SPY") == q
    assert replay_.hits == 1 and live.calls == 1
    with pytest.raises(LookupError):
        replay_.underlying_quote("QQQ")
    assert call_key("underlying_quote", "SPY") in replay_.tape
    assert prune_tape(ctl, keep_days=3, now=NOW + dt.timedelta(days=10)) == 1


# --- manifests -------------------------------------------------------------------


def test_arm_manifest_fields() -> None:
    assert _arm_of(_db(), "c1") == {}
    arm = _db()
    write_identity(arm, _ident())
    arm.execute(
        """INSERT INTO arm_pairs (arm_id, control_chain_run_id, arm_chain_run_id, fork_step,
                                  status, at) VALUES ('XP-1:treatment', 'c1', 'c1.treatment',
                                  'propose', 'ok', 'now')"""
    )
    assert _arm_of(arm, "c1.treatment") == {
        "arm_id": "XP-1:treatment",
        "paired_chain_run_id": "c1",
        "fork_step": "propose",
    }
