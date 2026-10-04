"""Experiment arm identity and broker keys (E10.2, D44).

An experiment arm is a second execution context of the unchanged trading loop:
its own store (``config/experiments.yaml`` ``runner.arms.<arm>.db``), its own
paper account (``<keys_env>_API_KEY`` / ``_SECRET_KEY``) and the spec arm's config
overlay. A store *is* an arm store when it holds the one-row ``arm_identity``
written by ``arc experiment start``; every component that builds a broker or
settings from a store reads it, so a process pointed at an arm store (the tick's
arm runner, a spawned Investor ladder, reconcile) can only trade the arm's account
and only under the arm's overlay. A store without an identity is control.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves ArmIdentity's annotations at runtime
import json
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import from_db, to_db
from arc.experiments.config import FORBIDDEN_ARM_KEYS

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ArmIdentity",
    "ArmKeyError",
    "arm_keys",
    "arm_stores",
    "read_identity",
    "write_identity",
]

_FORBID = ConfigDict(extra="forbid", frozen=True)


class ArmKeyError(RuntimeError):
    """The arm's broker keys are missing or are production/test keys."""


class ArmIdentity(BaseModel):
    """The ``arm_identity`` row of an arm store."""

    model_config = _FORBID

    arm_id: str = Field(..., description="'<experiment id>:<arm>'; the arm_id every row reports")
    experiment_id: str
    arm: str = Field(..., description="runner arm name (treatment, shadow_control, ...)")
    spec_arm: str = Field(..., description="the spec arm whose overlay it runs")
    keys_env: str
    control_db: str = Field(..., description="the control store this arm pairs with")
    overlay: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: _dt.datetime


def read_identity(conn: sqlite3.Connection) -> ArmIdentity | None:
    """The store's arm identity; ``None`` for control (and pre-E10 / unmigrated stores)."""
    try:
        r = conn.execute(
            """SELECT arm_id, experiment_id, arm, spec_arm, keys_env, control_db, overlay,
                      created_at FROM arm_identity WHERE id = 1"""
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if r is None:
        return None
    return ArmIdentity(
        arm_id=r[0],
        experiment_id=r[1],
        arm=r[2],
        spec_arm=r[3],
        keys_env=r[4],
        control_db=r[5],
        overlay=json.loads(r[6] or "{}"),
        created_at=from_db(r[7]),
    )


def write_identity(conn: sqlite3.Connection, ident: ArmIdentity) -> None:
    """Write the identity once (the table refuses updates and deletes)."""
    if ident.keys_env in FORBIDDEN_ARM_KEYS:
        msg = f"arm keys {ident.keys_env}_* are production/test keys"
        raise ArmKeyError(msg)
    with conn:
        conn.execute(
            """INSERT INTO arm_identity
               (id, arm_id, experiment_id, arm, spec_arm, keys_env, control_db, overlay,
                created_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ident.arm_id,
                ident.experiment_id,
                ident.arm,
                ident.spec_arm,
                ident.keys_env,
                ident.control_db,
                json.dumps(ident.overlay, sort_keys=True),
                to_db(ident.created_at),
            ),
        )


def arm_keys(keys_env: str, environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """``(<prefix>_API_KEY, <prefix>_SECRET_KEY)`` for an arm, never production/test keys.

    Raises :class:`ArmKeyError` when the prefix is ``ALPACA`` / ``ALPACA_TEST``, a key
    is unset, or the arm's key pair equals the production or test pair (the same
    account under another name).
    """
    env = os.environ if environ is None else environ
    if keys_env in FORBIDDEN_ARM_KEYS:
        msg = f"arm keys {keys_env}_* are production/test keys; arms use their own account"
        raise ArmKeyError(msg)
    key = env.get(f"{keys_env}_API_KEY", "")
    secret = env.get(f"{keys_env}_SECRET_KEY", "")
    if not key or not secret:
        msg = f"{keys_env}_API_KEY / {keys_env}_SECRET_KEY are not set (owner card E10.0)"
        raise ArmKeyError(msg)
    for other in sorted(FORBIDDEN_ARM_KEYS):
        if env.get(f"{other}_API_KEY") == key:
            msg = f"{keys_env}_API_KEY equals {other}_API_KEY: the arm must not share an account"
            raise ArmKeyError(msg)
    return key, secret


def arm_stores(conn: sqlite3.Connection) -> dict[str, Path]:
    """Arm name -> store path recorded in the control store at t0.

    Lives here (not in :mod:`arc.experiments.runner`) so the read path the tower
    uses (``evaluate`` -> ``paired``) never imports the runner's broker/routine code.
    """
    rows = conn.execute(
        "SELECT key, value FROM routine_state WHERE key LIKE 'experiment_arm:%' ORDER BY key"
    ).fetchall()
    return {r[0].split(":", 1)[1]: Path(r[1]) for r in rows}
