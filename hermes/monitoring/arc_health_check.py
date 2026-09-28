"""launchd runner for ``arc health check`` (E8.2).

Runs OUTSIDE the Hermes gateway on purpose: the gateway hosts the cron ticker,
so a check scheduled as a Hermes cron would die with the thing it watches.
``install.sh`` installs this as a user LaunchAgent (every 30 min).

- cwd = the repo checkout; runs its ``.venv/bin/arc health check``.
- Passes ``SLACK_BOT_TOKEN`` and ``ARC_*`` from ``~/.hermes/.env`` (same rule as
  the routines tick script); secrets are never printed or logged.
- ``arc health check`` records the ``health`` heartbeat, dedupes alerts and
  posts new/resolved ones to #project-arc itself. This wrapper only appends
  its report to ``data/logs/health-check.log`` (rotated, 5 MB x 3).
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(os.environ.get("ARC_REPO", Path.cwd()))
ARC = REPO / ".venv" / "bin" / "arc"
LOG = REPO / "data" / "logs" / "health-check.log"
HERMES_ENV = Path.home() / ".hermes" / ".env"
PASS_THROUGH = ("SLACK_BOT_TOKEN",)
PASS_PREFIXES = ("ARC_",)
TIMEOUT_S = 240
LOG_MAX_BYTES = 5_000_000
LOG_KEEP = 3


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for key, value in _dotenv(HERMES_ENV).items():
        if (key in PASS_THROUGH or key.startswith(PASS_PREFIXES)) and not env.get(key):
            env[key] = value
    env.setdefault("ARC_ENV", "paper")
    env["ARC_CRON_JOB"] = "arc-health-check"
    local_bin = str(Path.home() / ".local" / "bin")  # `hermes` for the gateway check
    if local_bin not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = os.pathsep.join([env.get("PATH", ""), local_bin]).strip(os.pathsep)
    return env


def _log(text: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size >= LOG_MAX_BYTES:
        for i in range(LOG_KEEP - 1, 0, -1):
            older = LOG.with_suffix(f".log.{i}")
            if older.exists():
                older.replace(LOG.with_suffix(f".log.{i + 1}"))
        LOG.replace(LOG.with_suffix(".log.1"))
    with LOG.open("a") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def main() -> int:
    started = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    if not ARC.is_file():
        _log(f"=== {started} {ARC} not found (run `uv sync` in {REPO})")
        return 2
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(ARC), "health", "check"],
            cwd=REPO,
            env=_env(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _log(f"=== {started} TIMEOUT after {TIMEOUT_S}s\n{exc.stdout or ''}{exc.stderr or ''}")
        return 3
    _log(f"=== {started} exit={proc.returncode}\n{proc.stdout}{proc.stderr}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
