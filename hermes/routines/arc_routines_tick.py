"""Hermes cron script: run ``arc routines tick`` once (D16, E5.3).

Installed by ``hermes/routines/install.sh`` as the ONE Hermes cron job for
Project Arc routines (``every 5m``, ``--no-agent``, ``--workdir <repo>``).
Every cadence, chain and trigger lives in ``config/routines.yaml``; this file
only runs the dispatcher.

Behaviour:

- cwd is the repo checkout (the cron job's ``--workdir``); the tick runs that
  checkout's ``.venv/bin/arc`` against its ``data/arc.db``.
- Only the variables Arc reads from the process environment (Slack bot token,
  Alpaca paper keys, ``ARC_*``) are passed through from ``~/.hermes/.env``; Arc's
  own settings read the rest of that file themselves. Nothing is printed.
- stdout is empty on success, so Hermes delivers nothing. Heartbeats and job
  failures are posted by Arc itself to the #arc-investor day thread.
- Exit 1 from ``arc routines tick`` means a job failed and was already alerted
  in the day thread: silent here too.
- Anything else (crash, timeout, missing venv) prints the error tail and exits
  non-zero, so Hermes delivers a cron failure alert (to #project-arc).
- Each tick's report is appended to ``data/logs/routines-tick.log`` (rotated at
  5 MB, 3 files kept).
- The tick refuses to run when the venv's editable ``arc`` install points at a
  different checkout (``uv sync`` through a symlinked ``.venv`` in a scratch
  worktree repoints it), because it would run foreign code against a foreign
  ``data/arc.db``. A ``.venv/bin/arc`` that is missing because ``uv sync`` is
  reinstalling it is retried for ``ARC_TICK_VENV_WAIT_SECONDS`` before alerting.
- E8.2: every tick gets an ``ARC_TICK_ID`` (and ``ARC_CRON_JOB``) in its
  environment. Arc binds it to every structured log line
  (``data/logs/arc.jsonl``), records it on the ``tick`` heartbeat and prints it,
  so a cron failure alert, a log line and a DB row share one id
  (``arc health trace <tick_id>``).
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path.cwd()
ARC = REPO / ".venv" / "bin" / "arc"
LOG = REPO / "data" / "logs" / "routines-tick.log"
HERMES_ENV = Path.home() / ".hermes" / ".env"
PASS_THROUGH = ("SLACK_BOT_TOKEN", "ALPACA_API_KEY", "ALPACA_SECRET_KEY")
PASS_PREFIXES = ("ARC_",)
TIMEOUT_S = int(os.environ.get("ARC_TICK_TIMEOUT_SECONDS", "3300"))
LOG_MAX_BYTES = 5_000_000
LOG_KEEP = 3
CRON_JOB = "arc-routines-tick"
VENV_WAIT_S = float(os.environ.get("ARC_TICK_VENV_WAIT_SECONDS", "60"))


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


def _env(tick_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env["ARC_TICK_ID"] = tick_id
    env["ARC_CRON_JOB"] = CRON_JOB
    for key, value in _dotenv(HERMES_ENV).items():
        if (key in PASS_THROUGH or key.startswith(PASS_PREFIXES)) and not env.get(key):
            env[key] = value
    env.setdefault("ARC_ENV", "paper")
    local_bin = str(Path.home() / ".local" / "bin")  # `hermes` for the persona LLM calls
    if local_bin not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = os.pathsep.join([env.get("PATH", ""), local_bin]).strip(os.pathsep)
    return env


def _rotate() -> None:
    if not LOG.exists() or LOG.stat().st_size < LOG_MAX_BYTES:
        return
    for i in range(LOG_KEEP - 1, 0, -1):
        older = LOG.with_suffix(f".log.{i}")
        if older.exists():
            older.replace(LOG.with_suffix(f".log.{i + 1}"))
    LOG.replace(LOG.with_suffix(".log.1"))


def _log(text: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    _rotate()
    with LOG.open("a") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


def _wait_for_arc() -> bool:
    deadline = time.monotonic() + VENV_WAIT_S
    while not ARC.is_file():
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)
    return True


def _foreign_editable_install() -> str | None:
    """Return the checkout the venv's editable ``arc`` points at, if it isn't REPO."""
    for pth in (REPO / ".venv" / "lib").glob("python*/site-packages/_editable_impl_arc*.pth"):
        for line in pth.read_text().splitlines():
            target = line.strip()
            if (
                target
                and not target.startswith(("#", "import "))
                and Path(target).resolve() != REPO.resolve()
            ):
                return target
    return None


def main() -> int:
    if not _wait_for_arc():
        sys.stdout.write(f"arc routines tick: {ARC} not found (run `uv sync` in {REPO})\n")
        return 2
    foreign = _foreign_editable_install()
    if foreign:
        sys.stdout.write(
            f"arc routines tick skipped: {REPO}/.venv's editable arc install points at "
            f"{foreign}, not {REPO}. Run `uv sync` in {REPO} to repair it.\n"
        )
        return 4
    started = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    tick_id = f"tick-{uuid.uuid4().hex[:12]}"
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(ARC), "routines", "tick"],
            cwd=REPO,
            env=_env(tick_id),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:  # removed between the check and the exec (uv sync race)
        sys.stdout.write(f"arc routines tick: {ARC} vanished mid-start (uv sync running?)\n")
        return 2
    except subprocess.TimeoutExpired as exc:
        _log(
            f"=== {started} {tick_id} TIMEOUT after {TIMEOUT_S}s\n"
            f"{exc.stdout or ''}{exc.stderr or ''}"
        )
        sys.stdout.write(f"arc routines tick {tick_id} timed out after {TIMEOUT_S}s (see {LOG})\n")
        return 3
    _log(f"=== {started} {tick_id} exit={proc.returncode}\n{proc.stdout}{proc.stderr}")
    if proc.returncode in (0, 1):  # 1 = a job failed; already alerted in the day thread
        return 0
    tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-20:])
    sys.stdout.write(
        f"arc routines tick {tick_id} crashed (exit {proc.returncode}):\n{tail}\n"
        f"trace: arc health trace {tick_id}\n"
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
