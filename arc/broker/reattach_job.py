"""Job ``broker.reattach`` (E11.2, D72): adopt the working order of a dead ladder.

Every tick in RTH (+10 min for DAY orders still settling), deterministic, no
LLM, halt-exempt. :func:`arc.execution.reattach.reattach` does the work; this
module builds its broker and lock manager the way the ladder does and turns the
report into one notice line per adopted (or wedged) execution, posted at once.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from arc.routines.handlers import JobResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.broker.base import BrokerAdapter
    from arc.execution.reattach import ReattachReport
    from arc.routines.handlers import JobContext
    from arc.routines.locks import LockManager

__all__ = ["reattach_job", "reattach_notice", "reattach_step"]


def reattach_notice(report: ReattachReport) -> str:
    """One line per adopted / wedged execution; empty when there is nothing to say."""
    lines = [a.alert for a in report.adopted]
    if report.wedged:
        term = ""
        if report.terminated:
            term = f"; SIGTERM sent to pid {', '.join(map(str, report.terminated))}"
        lines.append(
            f"{len(report.wedged)} ladder(s) hold their lock but stopped beating "
            f"({', '.join(h[:12] for h in report.wedged)}){term}"
        )
    lines.extend(f"re-attach error: {e}" for e in report.errors)
    return "\n".join(lines)


def reattach_job(
    ctx: JobContext,
    *,
    broker: BrokerAdapter,
    locks: LockManager,
    sleep: Callable[[float], None] = time.sleep,
) -> JobResult:
    from arc.execution.reattach import reattach

    report = reattach(
        ctx.conn,
        broker,
        locks,
        now=ctx.clock(),
        settings=ctx.settings,
        run_id=ctx.run_id,
        clock=ctx.clock,
        sleep=sleep,
    )
    return JobResult(
        summary=report.summary(),
        notice=reattach_notice(report),
        metrics={
            "checked": report.checked,
            "orphans": len(report.orphans),
            "adopted": len(report.adopted),
            "alive": len(report.skipped_alive),
            "wedged": len(report.wedged),
            "unconfirmed": int(report.unconfirmed),
            "errors": len(report.errors),
        },
    )


def reattach_step(ctx: JobContext) -> JobResult:
    """Dispatcher entry point: the store's broker (E10.2: arms adopt on their account).

    Uses the dispatcher's lock dir; without one (a dry run) orphans are only listed.
    """
    from arc.experiments.broker import trading_broker
    from arc.routines.locks import LockManager, NullLocks

    lock_dir = ctx.run_env.lock_dir
    locks = LockManager(lock_dir) if lock_dir else NullLocks()
    return reattach_job(ctx, broker=trading_broker(ctx.conn, ctx.settings), locks=locks)
