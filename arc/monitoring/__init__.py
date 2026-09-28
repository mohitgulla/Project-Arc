"""Ops monitoring (E8.2): heartbeats, routine-window watchdog, gateway health,
rotated structured logs, ops alerts and run-id correlation.

Modules:

- :mod:`arc.monitoring.config`       ``monitoring:`` section of ``config/routines.yaml``
- :mod:`arc.monitoring.correlation`  tick/cron/kanban/session ids bound to every log line
- :mod:`arc.monitoring.logs`         JSON-lines log file with size-based rotation
- :mod:`arc.monitoring.store`        ``heartbeats`` and ``ops_alerts`` tables
- :mod:`arc.monitoring.checks`       pure checks: missed windows, stale tick, stuck runs, gateway
- :mod:`arc.monitoring.alerts`       dedupe/resolve alerts and post them to Slack
- :mod:`arc.monitoring.cli`          ``arc health check|status|trace``

Kept import-free here so :mod:`arc.routines.config` can import the config
model without a cycle.
"""
