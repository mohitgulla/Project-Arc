"""E10.2 integration: the experiment arm's paper account (``ALPACA_EXP_*``, E10.0).

Read-only: connects through :func:`arc.experiments.broker.trading_broker` on an arm
store, so the keys come from the arm's identity (never production/test), checks
``/v2/account`` and that the account is flat. It places no order (RTH order
evidence is card E10.2a). Skips with a reason when the keys are not set; a skip
is not a pass.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration


def _exp_keys() -> tuple[str, str] | None:
    key, secret = os.environ.get("ALPACA_EXP_API_KEY"), os.environ.get("ALPACA_EXP_SECRET_KEY")
    return (key, secret) if key and secret else None


@pytest.mark.skipif(_exp_keys() is None, reason="ALPACA_EXP_API_KEY/SECRET_KEY not set (E10.0)")
def test_arm_store_connects_to_the_experiment_account_only(tmp_path) -> None:  # noqa: ANN001
    from arc.config import ArcSettings
    from arc.experiments.arms import ArmIdentity, read_identity, write_identity
    from arc.experiments.broker import trading_broker
    from arc.experiments.virtual import open_account
    from arc.store.migrate import migrate
    from arc.utils.calendar import now_et

    prod = os.environ.get("ALPACA_API_KEY")
    assert prod is None or prod != os.environ["ALPACA_EXP_API_KEY"], "EXP key equals production"
    now = now_et()
    arm = sqlite3.connect(str(tmp_path / "arm.db"))
    arm.row_factory = sqlite3.Row
    migrate(arm)
    write_identity(
        arm,
        ArmIdentity(
            arm_id="X-0:treatment", experiment_id="X-0", arm="treatment",
            spec_arm="treatment", keys_env="ALPACA_EXP",
            control_db=str(tmp_path / "missing-control.db"), overlay={}, created_at=now,
        ),
    )  # fmt: skip
    open_account(arm, "X-0:treatment", t0_equity=Decimal(10000), legacy={}, at=now)
    assert read_identity(arm) is not None
    s = ArcSettings(account_profile="cash_debit")  # type: ignore[call-arg]
    broker = trading_broker(arm, s, now=lambda: now)
    inner = broker.inner  # type: ignore[attr-defined]
    raw = inner.account()
    assert raw.equity > 0 and raw.options_approved_level is not None
    assert inner.positions() == [], "experiment account must be flat before t0"
    virtual = broker.account()
    assert virtual.equity == Decimal(10000)  # the arm sizes from its virtual account
    assert virtual.buying_power <= Decimal(10000)
    since = now - dt.timedelta(days=1)
    _ = inner.option_orders_since(since)  # read-only listing works with the arm keys
