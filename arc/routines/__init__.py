"""Config-driven routine dispatcher (D16): cadences, chains, triggers.

See :mod:`arc.routines.dispatcher` for the tick algorithm and
``config/routines.yaml`` for the schedule itself.
"""

from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    JobKind,
    JobSpec,
    RoutinesConfig,
    StepSpec,
    load_routines,
)
from arc.routines.dispatcher import Dispatcher, Outcome, TickReport
from arc.routines.handlers import JobContext, JobResult, JobSkippedError

__all__ = [
    "DEFAULT_ROUTINES_PATH",
    "Dispatcher",
    "JobContext",
    "JobKind",
    "JobResult",
    "JobSkippedError",
    "JobSpec",
    "Outcome",
    "RoutinesConfig",
    "StepSpec",
    "TickReport",
    "load_routines",
]
