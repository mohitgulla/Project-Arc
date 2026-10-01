"""``monitoring:`` section of ``config/routines.yaml`` (E8.2).

Every threshold is config; changing one is a YAML edit validated by
``arc routines validate``.
"""

from __future__ import annotations

import datetime as _dt
import enum
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arc.context.ttl import parse_duration


def _dur(v: Any) -> Any:
    return parse_duration(v) if isinstance(v, str) else v


class AlertChannel(enum.StrEnum):
    """Where ops alerts go. ``project_arc`` is the ops/dev channel (same as cron failures)."""

    PROJECT_ARC = "project_arc"
    ARC_INVESTOR = "arc_investor"


class GatewayCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    hermes_bin: str = "hermes"
    timeout: _dt.timedelta = _dt.timedelta(seconds=30)
    alert_on_degraded: bool = False  # warnings (e.g. stale launchd plist) are recorded only

    @field_validator("timeout", mode="before")
    @classmethod
    def _timeout(cls, v: Any) -> Any:
        return _dur(v)


class RemoteAccessCheck(BaseModel):
    """E8.6: Hermes dashboard (:1994, basic auth) and tower (:4174) on the tailnet only.

    Probes: dashboard ``GET /api/status`` (auth required, ``basic``); tower v2
    ``GET /api/health`` (200 + ``status: ok``, D35); neither port open on a LAN IP.

    Off until the owner has installed Tailscale and the two LaunchAgents
    (``hermes/remote/install.sh``); then set ``enabled: true``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    dashboard_port: Annotated[int, Field(ge=1, le=65535)] = 1994
    tower_port: Annotated[int, Field(ge=1, le=65535)] = 4174
    timeout: _dt.timedelta = _dt.timedelta(seconds=5)

    @field_validator("timeout", mode="before")
    @classmethod
    def _timeout(cls, v: Any) -> Any:
        return _dur(v)


class LogSettings(BaseModel):
    """Structured JSON-lines log written by unattended commands (tick, health)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Relative paths resolve against the DB's directory (data/ by default), so a
    # scratch --db keeps its logs next to it.
    path: Path = Path("logs/arc.jsonl")
    max_bytes: Annotated[int, Field(ge=10_000)] = 5_000_000
    backups: Annotated[int, Field(ge=1, le=50)] = 5


class MonitoringSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # No tick heartbeat for this long -> `tick_stale` alert (3 missed 5-min ticks).
    tick_stale_after: _dt.timedelta = _dt.timedelta(minutes=15)
    # A slot is only judged after its catch-up deadline + this grace.
    miss_grace: _dt.timedelta = _dt.timedelta(minutes=10)
    # How far back the watchdog looks for missed slots.
    miss_lookback: _dt.timedelta = _dt.timedelta(days=1)
    # A routine_runs row still `running` after this long is reported as stuck.
    stuck_after: _dt.timedelta = _dt.timedelta(minutes=70)
    # Per-job override of ``stuck_after`` (E5.3a: ``monitor: 10m`` at a 5-min
    # cadence). Keys are job or chain-step names, checked by RoutinesConfig.
    stuck_after_jobs: dict[str, _dt.timedelta] = Field(default_factory=dict)
    alert_channel: AlertChannel = AlertChannel.PROJECT_ARC
    gateway: GatewayCheck = Field(default_factory=GatewayCheck)
    remote_access: RemoteAccessCheck = Field(default_factory=RemoteAccessCheck)
    log: LogSettings = Field(default_factory=LogSettings)

    @field_validator(
        "tick_stale_after", "miss_grace", "miss_lookback", "stuck_after", mode="before"
    )
    @classmethod
    def _durations(cls, v: Any) -> Any:
        return _dur(v)

    @field_validator("stuck_after_jobs", mode="before")
    @classmethod
    def _job_durations(cls, v: Any) -> Any:
        if v is None:
            return {}
        if isinstance(v, dict):
            return {k: _dur(d) for k, d in v.items()}
        return v

    @field_validator("stuck_after_jobs")
    @classmethod
    def _positive(cls, v: dict[str, _dt.timedelta]) -> dict[str, _dt.timedelta]:
        for job, d in v.items():
            if d <= _dt.timedelta(0):
                msg = f"stuck_after_jobs.{job} must be positive"
                raise ValueError(msg)
        return v

    def stuck_after_for(self, job: str) -> _dt.timedelta:
        """``stuck_after`` for *job*: its per-job override, else the global value."""
        return self.stuck_after_jobs.get(job, self.stuck_after)
