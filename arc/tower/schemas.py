"""Response models for the v2 control tower API (E8.7, D35).

Every response is a pydantic model with ``extra="forbid"`` and carries ``as_of``
(ET, ISO 8601). The snapshot endpoint returns :class:`arc.tower.data.TowerSnapshot`
as-is, so the page cards in E8.7a–d start from the same views as the Streamlit page.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations at runtime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "REFRESH_CHOICES",
    "AppInfo",
    "Cadence",
    "ErrorResponse",
    "GateCaps",
    "HealthResponse",
    "MetaResponse",
]

REFRESH_CHOICES: tuple[int, ...] = (30, 60, 120)
"""Client polling intervals the Settings menu offers, seconds (TOWER_DESIGN §7)."""

STALE_FACTOR = 3
"""A number is stale once its age exceeds this many cadences of its producer (§3)."""

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ErrorResponse(BaseModel):
    """Every non-2xx response: ``{error, detail, as_of}``."""

    model_config = _STRICT

    error: str = Field(description="Stable machine code, e.g. db_unavailable, not_found")
    detail: str = Field(description="Human-readable explanation")
    as_of: _dt.datetime


class HealthResponse(BaseModel):
    model_config = _STRICT

    status: Literal["ok"]
    db: str = Field(description="Audit store path, opened read-only")
    as_of: _dt.datetime


class AppInfo(BaseModel):
    model_config = _STRICT

    name: Literal["arc"] = "arc"
    version: str
    git_sha: str | None = None


class Cadence(BaseModel):
    """How often a producing job runs and when its output counts as stale."""

    model_config = _STRICT

    job: str
    label: str = Field(description="Human cadence, e.g. 'every 30m 09:30-16:00 ET (trading)'")
    every_s: int = Field(description="Nominal seconds between runs")
    stale_after_s: int = Field(description=f"{STALE_FACTOR} x every_s")
    window: str | None = Field(default=None, description="Intraday window (ET), if any")
    days: str | None = Field(default=None, description="daily | trading | weekdays | mon,fri…")


class GateCaps(BaseModel):
    """The gate's portfolio caps (PLAN §5), from the effective settings."""

    model_config = _STRICT

    portfolio_delta_cap: float = Field(description="|net Δ| cap: × equity/100, share-eq")
    portfolio_vega_cap_pct: float = Field(description="|ν| cap as a fraction of equity")


class MetaResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    app: AppInfo
    env: str = Field(description="ARC_ENV (paper | live)")
    account_profile: str
    config_version: int = Field(description="Latest D26 config_changes id (0 = none)")
    refresh_interval_s: int = Field(description="Default client poll interval")
    refresh_choices_s: list[int] = Field(default_factory=lambda: list(REFRESH_CHOICES))
    lookback_days: int
    cadences: dict[str, Cadence] = Field(description="monitor, auditor, tick")
    gate_caps: GateCaps
    theme_default: Literal["system", "light", "dark"] = "system"
