"""Experiment arm identity and broker keys (E10.2, D44).

An experiment arm is a second execution context of the unchanged trading loop:
its own store (``config/experiments.yaml`` ``runner.arms.<arm>.db``), its own
paper account (``<keys_env>_API_KEY`` / ``_SECRET_KEY``) and the spec arm's config
overlay. A store *is* an arm store when it holds the one-row ``arm_identity``
written by ``arc experiment start``; every component that builds a broker or
settings from a store reads it, so a process pointed at an arm store (the tick's
arm runner, a spawned Broker ladder, reconcile) can only trade the arm's account
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
    "STATE_ARM",
    "ArmAccountChangedError",
    "ArmIdentity",
    "ArmKeyError",
    "account_fingerprint",
    "arm_keys",
    "arm_stores",
    "default_environ",
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
    plan: dict[str, Any] | None = Field(
        default=None,
        description=(
            "E13.12: the arm's ArmPlan as JSON (arc.experiments.models.ArmPlan); None on "
            "a store created before E13.12 (recomputed from the overlay)"
        ),
    )
    # D69 (E15.1, migration 033): how the arm's account is used and which account it is.
    account_mode: str = Field(
        default="dedicated", description="dedicated | shared (runner.account_mode at t0)"
    )
    account_sha256: str | None = Field(
        default=None,
        description="sha256 of the broker account_number at t0 (never the raw number)",
    )
    account_last4: str | None = Field(default=None, description="last 4 of the account_number")


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
    try:  # E13.12 (migration 026); a store not migrated yet has no plan column
        p = conn.execute("SELECT plan FROM arm_identity WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        p = None
    try:  # D69 (migration 033)
        a = conn.execute(
            "SELECT account_mode, account_sha256, account_last4 FROM arm_identity WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        a = None
    return ArmIdentity(
        arm_id=r[0],
        experiment_id=r[1],
        arm=r[2],
        spec_arm=r[3],
        keys_env=r[4],
        control_db=r[5],
        overlay=json.loads(r[6] or "{}"),
        created_at=from_db(r[7]),
        plan=json.loads(p[0]) if p is not None and p[0] else None,
        account_mode=(a[0] if a is not None and a[0] else "dedicated"),
        account_sha256=a[1] if a is not None else None,
        account_last4=a[2] if a is not None else None,
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
                created_at, plan, account_mode, account_sha256, account_last4)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ident.arm_id,
                ident.experiment_id,
                ident.arm,
                ident.spec_arm,
                ident.keys_env,
                ident.control_db,
                json.dumps(ident.overlay, sort_keys=True),
                to_db(ident.created_at),
                None if ident.plan is None else json.dumps(ident.plan, sort_keys=True),
                ident.account_mode,
                ident.account_sha256,
                ident.account_last4,
            ),
        )


def account_fingerprint(account_number: str) -> tuple[str, str]:
    """``(sha256 hex, last 4)`` of a broker account number: all an arm ever stores (D69)."""
    import hashlib

    return hashlib.sha256(account_number.encode()).hexdigest(), account_number[-4:]


class ArmAccountChangedError(RuntimeError):
    """D69: the arm's keys now reach a different broker account than at t0."""


HERMES_ENV_FILE = Path.home() / ".hermes" / ".env"


def default_environ(env_file: Path | None = None) -> dict[str, str]:
    """``~/.hermes/.env`` overlaid by ``os.environ`` (the process wins).

    The same sources :class:`arc.config.ArcSettings` reads the production keys from:
    the routines tick (cron) and its detached ``arms-tick`` do not export the file, so
    reading ``os.environ`` alone left the arm without keys (E10.2b).
    """
    path = HERMES_ENV_FILE if env_file is None else env_file
    merged: dict[str, str] = {}
    if path.is_file():
        from dotenv import dotenv_values

        merged.update({k: v for k, v in dotenv_values(path).items() if v})
    merged.update(os.environ)
    return merged


def arm_keys(keys_env: str, environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """``(<prefix>_API_KEY, <prefix>_SECRET_KEY)`` for an arm, never production/test keys.

    *environ* defaults to :func:`default_environ` (``~/.hermes/.env`` + ``os.environ``).
    Raises :class:`ArmKeyError` when the prefix is ``ALPACA`` / ``ALPACA_TEST``, a key
    is unset, or the arm's key pair equals the production or test pair (the same
    account under another name).
    """
    env = default_environ() if environ is None else environ
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


#: control ``routine_state`` key of an arm store: ``experiment_arm:<experiment id>:<arm>``
#: (D69: concurrent experiments may reuse arm names). Stores started before D69 carry
#: the legacy ``experiment_arm:<arm>``; their experiment is read from the store.
STATE_ARM = "experiment_arm:{experiment_id}:{arm}"
_STATE_PREFIX = "experiment_arm:"


def _store_experiment(path: Path) -> str | None:
    """The experiment of the arm store at *path* (read-only), or None."""
    if not path.is_file():
        return None
    try:
        c = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        r = c.execute("SELECT experiment_id FROM arm_identity WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    return str(r[0]) if r is not None else None


def arm_stores(conn: sqlite3.Connection, experiment_id: str | None = None) -> dict[str, Path]:
    """Arm store paths recorded in the control store at t0.

    With *experiment_id*: ``{arm name: path}`` of that experiment. Without: every arm
    store, keyed ``<experiment id>:<arm>`` (a pre-D69 legacy key keeps its bare arm
    name). Lives here (not in :mod:`arc.experiments.runner`) so the read path the tower
    uses (``evaluate`` -> ``paired``) never imports the runner's broker/routine code.
    """
    rows = conn.execute(
        "SELECT key, value FROM routine_state WHERE key LIKE 'experiment_arm:%' ORDER BY key"
    ).fetchall()
    out: dict[str, Path] = {}
    for key, value in rows:
        rest = str(key)[len(_STATE_PREFIX) :]
        xp, sep, arm = rest.rpartition(":")
        path = Path(value)
        if experiment_id is None:
            out[rest] = path
        elif sep and xp == experiment_id:
            out[arm] = path
        elif not sep and _store_experiment(path) == experiment_id:
            out[rest] = path
    return out
