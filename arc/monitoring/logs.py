"""Structured logs for unattended commands (E8.2): JSON lines, size-rotated.

:func:`configure` sets the structlog chain used by ``arc routines tick|run`` and
``arc health check``:

1. ``merge_contextvars``: correlation ids bound with
   :func:`arc.monitoring.correlation.bind` (tick_id, run_id, ...) join every line.
2. ``add_log_level`` and an ET ISO timestamp (``ts``) from :mod:`arc.utils.calendar`.
3. A tee that writes the line as JSON to a :class:`~logging.handlers.RotatingFileHandler`
   (``max_bytes``, ``backups`` from ``monitoring.log``). ``debug`` lines stay out of
   the file.
4. The human console renderer on stderr (unchanged behaviour: stdout carries the report).

Log writes are best-effort: a full disk or unwritable path never fails a job.
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import sys
from typing import TYPE_CHECKING, Any

import structlog

from arc.utils.calendar import now_et

if TYPE_CHECKING:
    from pathlib import Path

    from structlog.typing import EventDict, WrappedLogger

    from arc.monitoring.config import LogSettings

FILE_LOGGER = "arc.jsonl"
_SKIP_LEVELS = frozenset({"debug"})


def _stderr_logger(*_args: object) -> structlog.PrintLogger:
    # Resolve sys.stderr at log time so a replaced stream (test capture) is never stale.
    return structlog.PrintLogger(file=sys.stderr)


def _add_ts(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    event_dict.setdefault("ts", now_et().isoformat(timespec="milliseconds"))
    return event_dict


class JsonFileTee:
    """structlog processor: append the event as one JSON line, then pass it on."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def __call__(self, _logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
        if event_dict.get("level") not in _SKIP_LEVELS:
            # Logging must never fail a job.
            with contextlib.suppress(Exception):
                self._logger.info(json.dumps(event_dict, default=str, sort_keys=True))
        return event_dict


def file_logger(path: Path, *, max_bytes: int, backups: int) -> logging.Logger:
    """The stdlib logger behind the tee, with exactly one rotating handler on *path*."""
    logger = logging.getLogger(FILE_LOGGER)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def processors(tee: JsonFileTee | None) -> list[Any]:
    chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _add_ts,
    ]
    if tee is not None:
        chain.append(tee)
    chain.append(structlog.dev.ConsoleRenderer(colors=False))
    return chain


def configure(settings: LogSettings | None, *, base_dir: Path | None) -> Path | None:
    """Console logs on stderr, plus the rotated JSON file when *base_dir* is given.

    ``settings.path`` is relative to *base_dir* (the directory holding the DB,
    ``data/`` by default) unless absolute. Returns the file path, or ``None``
    when file logging is off (in-memory/dry runs) or the file can't be opened.
    """
    tee: JsonFileTee | None = None
    path: Path | None = None
    if settings is not None and base_dir is not None:
        path = settings.path if settings.path.is_absolute() else base_dir / settings.path
        try:
            tee = JsonFileTee(
                file_logger(path, max_bytes=settings.max_bytes, backups=settings.backups)
            )
        except OSError as exc:
            sys.stderr.write(f"arc: file logging disabled ({exc})\n")
            path = None
    structlog.configure(processors=processors(tee), logger_factory=_stderr_logger)
    return path
