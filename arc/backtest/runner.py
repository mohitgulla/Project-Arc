"""The nightly backtest study runner (card E26.3, PLAN D121): routine ``backtest.studies``.

Every night from 21:30 ET (``backtest_studies.window_start``) until 08:30 ET
(``window_end``; a night followed by a weekend or holiday runs until
``weekend_window_end`` on the next session, normally Mon 08:30) the runner works the
study queue (``data/backtest/queue.json``, :mod:`arc.backtest.queue`) one study at a
time:

1. Wait for a still-running ``history.topup`` (up to ``topup_wait``), else skip the night.
2. Pick the oldest ``queued`` / ``running`` entry whose spec is on ``origin/main``
   (``config/backtest_studies/BT-<n>.yaml``); one that isn't merged yet is passed over
   (``skipped: spec_unmerged``). A committed spec whose hash differs from the queued
   one marks the entry ``failed``. The slot runs on trading days, so a night that spans
   a weekend or holiday is the only run until the next session: with nothing to run it
   re-reads the queue every ``idle_poll`` instead of ending, so a study approved on
   Saturday starts that night.
3. Run its next rung, ``c125_dev`` -> ``c500_dev`` -> ``holdout`` (on the spec's last
   ladder universe), through ``arc backtest study run`` in a niced child with
   ``workers`` processes. Before each run the time left must cover the estimate (the
   median ``runtime_s`` of this study's ledger rows for the rung, else every study's,
   x ``runtime_margin``); otherwise the night ends ``deferred: window``.
4. At the window end (or on an owner ``stop BT-<n>``) the child gets SIGTERM, then
   SIGKILL after ``kill_grace``. ``study run`` exits on SIGTERM without a ledger line,
   so the rung is ``interrupted`` and retried the next night (the menu cache keeps the
   repeat cheap).
5. A dev rung that **clearly fails** (every treatment on every profile a non-switch
   call with its CI upper bound below ``fail_rule.ci_hi_below``) stops the study:
   ``stopped`` with the numbers in ``stop_reason``, and no holdout is spent. Otherwise
   the ladder goes on; the holdout concludes the study.
6. A non-zero exit that isn't an interrupt marks the entry ``failed`` (the log is in
   ``<runs_dir>/<BT>/runner.log``); the queue moves on, no retry.

One #arc-analyst message per event: rung done, ``stopped: dev fail``, ``failed`` and the
holdout verdict. Nights that only defer or skip post nothing.

Everything time- and process-related is injected (clock, sleep, launcher, the
``origin/main`` view, the Slack poster), so the tests never read the wall clock.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog
import yaml

from arc.backtest.queue import (
    RUNG_RUNS,
    RUNGS,
    QueueEntry,
    QueueRun,
    load_queue,
    update_queue,
)
from arc.backtest.study import (
    LedgerEntry,
    StudySpec,
    load_ledger,
    study_hash,
    verdict_lines,
)
from arc.utils.calendar import ET, next_session

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from arc.experiments.publish import MainView
    from arc.routines.config import BacktestStudiesSettings

__all__ = [
    "RECENT_ROWS",
    "SPECS_DIR",
    "NightResult",
    "RunHandle",
    "RunnerPaths",
    "SkipNightError",
    "clear_dev_fail",
    "estimate_s",
    "night_window",
    "popen_launcher",
    "rung_argv",
    "rung_line",
    "run_night",
]

log = structlog.get_logger(__name__)

SPECS_DIR = "config/backtest_studies"
#: Recent ledger rows a rung's runtime estimate is the median of.
RECENT_ROWS = 5
#: Exit code of ``study run`` on SIGTERM (128 + 15).
INTERRUPTED_RC = 143
_LOG_NAME = "runner.log"
_TAIL_LINES = 40


class SkipNightError(RuntimeError):
    """Nothing runs tonight (outside the window, ``history.topup`` still running)."""


# ---------------------------------------------------------------------------
# Window maths (ET)
# ---------------------------------------------------------------------------


def _elapsed(a: dt.datetime, b: dt.datetime) -> dt.timedelta:
    """Real time from *a* to *b*. Subtracting two datetimes of the same zone is wall-clock
    time in Python (a DST night would be off by an hour), so go through UTC."""
    return b.astimezone(dt.UTC) - a.astimezone(dt.UTC)


def _night_end(day: dt.date, cfg: BacktestStudiesSettings) -> dt.datetime:
    """End of the night that starts at ``window_start`` on *day*: ``window_end`` on the
    next session, or ``weekend_window_end``'s time when that session is not *day* + 1
    (a weekend or holiday in between)."""
    nxt = next_session(day)
    t = cfg.window_end if nxt == day + dt.timedelta(days=1) else cfg.weekend_end_time
    return dt.datetime.combine(nxt, t, tzinfo=ET)


def night_window(
    now: dt.datetime, cfg: BacktestStudiesSettings
) -> tuple[dt.datetime, dt.datetime] | None:
    """``(start, end)`` of the study window holding *now* (ET), or ``None`` outside it.

    A window opens at ``window_start`` every calendar day (weekends too) and closes at
    :func:`_night_end`; so between ``window_end`` and ``window_start`` on a trading day
    nothing is open, while a weekend day sits inside Friday's long night.
    """
    t = now.astimezone(ET)
    for back in range(7):
        day = t.date() - dt.timedelta(days=back)
        start = dt.datetime.combine(day, cfg.window_start, tzinfo=ET)
        if start <= t < _night_end(day, cfg):
            return start, _night_end(day, cfg)
    return None


# ---------------------------------------------------------------------------
# Estimates, rungs, the clear-fail rule
# ---------------------------------------------------------------------------


def _measured(e: LedgerEntry) -> float | None:
    from arc.backtest.queue import measured_runtime_s

    return measured_runtime_s(e)


def estimate_s(
    entries: Sequence[LedgerEntry], study: str, phase: str, universe: str, margin: float
) -> float | None:
    """Seconds a rung is expected to take: the median measured runtime of the last
    :data:`RECENT_ROWS` ledger rows of *study* on (*phase*, *universe*), else of every
    study's rows there, x *margin*. ``None`` without any row (the run goes ahead)."""
    rows = sorted((e for e in entries if e.phase == phase and e.universe == universe), key=_at)
    for pool in ([e for e in rows if e.study == study], rows):
        secs = [s for e in pool if (s := _measured(e)) is not None][-RECENT_ROWS:]
        if secs:
            return float(statistics.median(secs)) * margin
    return None


def _at(e: LedgerEntry) -> dt.datetime:
    return e.at


def rung_run(rung: str, spec: StudySpec) -> tuple[str, str] | None:
    """``(phase, universe)`` the rung runs on for *spec*; ``None`` when the spec's
    ladder lacks the rung's universe (a dev rung it never planned: skipped). The
    holdout always runs on the spec's last ladder universe (C500 normally, D121)."""
    phase, uni = RUNG_RUNS[rung]
    if phase == "holdout":
        return phase, spec.universe_ladder[-1]
    return (phase, uni) if uni in spec.universe_ladder else None


def clear_dev_fail(entry: LedgerEntry, ci_hi_below: float) -> bool:
    """D121: every treatment on every profile is a non-switch call, its bootstrap CI
    upper bound (daily P&L difference treatment - control, the only CI a verdict row
    carries) is below *ci_hi_below*, and its primary-metric difference is negative
    (clearly worse on both). No verdict rows is never a fail."""
    rows = [v for tr in entry.verdicts.values() for v in tr.values()]
    return bool(rows) and all(
        not v.get("switch")
        and float(v.get("ci_hi", 0.0)) < ci_hi_below
        and float(v.get("primary_diff", 0.0)) < 0
        for v in rows
    )


def _fmt(x: Any) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return str(x)


def rung_line(entry: LedgerEntry) -> str:
    """``profile/treatment: call, diff X, CI [lo, hi]`` per row, joined with ``; ``."""
    parts = []
    for p, tr in sorted(entry.verdicts.items()):
        for t, v in sorted(tr.items()):
            call = v.get("call") or ("switch candidate" if v.get("switch") else "keep control")
            parts.append(
                f"{p}/{t}: {call}, diff {_fmt(v.get('pnl_diff'))}, "
                f"CI [{_fmt(v.get('ci_lo'))}, {_fmt(v.get('ci_hi'))}]"
            )
    return "; ".join(parts) or "no verdict rows"


def _fail_numbers(entry: LedgerEntry) -> str:
    parts = []
    for p, tr in sorted(entry.verdicts.items()):
        for t, v in sorted(tr.items()):
            parts.append(
                f"{p}/{t} switch={v.get('switch')} primary_diff {v.get('primary_diff')} "
                f"CI [{v.get('ci_lo')}, {v.get('ci_hi')}]"
            )
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# The child process
# ---------------------------------------------------------------------------


class RunHandle(Protocol):
    """A running ``arc backtest study run`` child."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...  # SIGTERM to the study process
    def kill(self) -> None: ...  # SIGKILL to its whole process group


@dataclass
class _PopenHandle:
    proc: subprocess.Popen[bytes]

    @property
    def pid(self) -> int:
        return self.proc.pid

    def poll(self) -> int | None:
        return self.proc.poll()

    def terminate(self) -> None:
        self.proc.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)


def popen_launcher(argv: Sequence[str], log_path: Path, cwd: Path) -> RunHandle:
    """Start *argv* in its own session (so SIGKILL reaches the pool workers), stdout and
    stderr appended to *log_path*."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as fh:
        proc = subprocess.Popen(  # noqa: S603 - argv is built from our own constants
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
        )
    return _PopenHandle(proc)


@dataclass(frozen=True)
class RunnerPaths:
    """Absolute paths the runner reads and writes."""

    queue: Path
    ledger: Path
    runs_dir: Path
    report_dir: Path | None
    data_dir: Path
    cwd: Path


def rung_argv(
    study_id: str,
    *,
    phase: str,
    universe: str,
    studies_dir: Path,
    paths: RunnerPaths,
    workers: int,
    nice: int,
    now: dt.datetime,
    python: str = sys.executable,
) -> list[str]:
    """``[nice -n N] python -c 'arc main' backtest study run BT-<n> ...``."""
    argv = [python, "-c", "from arc.cli import main; raise SystemExit(main())"]
    argv += ["backtest", "study", "run", study_id, "--studies-dir", str(studies_dir)]
    argv += ["--ledger", str(paths.ledger), "--runs-dir", str(paths.runs_dir)]
    argv += ["--data-dir", str(paths.data_dir), "--workers", str(workers)]
    argv += ["--universe", universe, "--now", now.isoformat()]
    argv += ["--report-dir", str(paths.report_dir)] if paths.report_dir else ["--no-report"]
    if phase == "holdout":
        argv.append("--holdout")
    nice_bin = shutil.which("nice") if nice else None
    return [nice_bin, "-n", str(nice), *argv] if nice_bin else argv


def _tail(path: Path, n: int = _TAIL_LINES) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# One night
# ---------------------------------------------------------------------------


@dataclass
class NightResult:
    """What the night did: one line per action, the Slack messages, metrics."""

    lines: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return "; ".join(self.lines) or "nothing to run"


@dataclass
class _Night:
    cfg: BacktestStudiesSettings
    paths: RunnerPaths
    clock: Callable[[], dt.datetime]
    sleep: Callable[[float], None]
    main: MainView
    launcher: Callable[[Sequence[str], Path, Path], RunHandle]
    post: Callable[[str], None]
    heartbeat: Callable[[], None]
    end: dt.datetime
    python: str
    #: E26.3: a night that spans a weekend/holiday waits for new work (see ``run``)
    idle: bool = False
    res: NightResult = field(default_factory=NightResult)

    # -- queue helpers ------------------------------------------------------

    def _update(self, study_id: str, fn: Callable[[QueueEntry], None]) -> QueueEntry | None:
        """Apply *fn* to the entry if it is still active (an owner stop wins)."""

        def go(q: Any) -> QueueEntry | None:
            e = q.get(study_id)
            if e is None or e.status not in ("queued", "running"):
                return e
            fn(e)
            return e

        return update_queue(self.paths.queue, go)

    def _emit(self, line: str, message: str | None = None) -> None:
        self.res.lines.append(line)
        if message is not None:
            self.res.messages.append(message)
            try:
                self.post(message)
            except Exception as exc:  # noqa: BLE001 - a Slack outage never fails the night
                log.warning("backtest.runner.post_failed", error=str(exc))

    def _committed_spec(self, study_id: str) -> str | None:
        return self.main.files(SPECS_DIR).get(f"{SPECS_DIR}/{study_id}.yaml")

    # -- the loop -----------------------------------------------------------

    def run(self) -> NightResult:
        done = 0
        unmerged: set[str] = set()
        while True:
            if self.clock() >= self.end:
                self.res.lines.append("window closed")
                break
            pick = self._pick(unmerged)
            if pick is None:
                if not self.idle:
                    break
                # a weekend/holiday night is the only run until the next session: wait
                # for a study approved meanwhile (or a spec that merges) instead of ending
                self.heartbeat()
                self.sleep(self.cfg.idle_poll.total_seconds())
                unmerged.clear()
                continue
            entry, text = pick
            spec = self._spec(entry, text)
            if spec is None:
                continue
            rung = self._advance(entry, spec)
            if rung is None:
                continue
            phase, uni = rung
            outcome = self._run_rung(entry, spec, phase, uni)
            done += outcome == "done"
            if outcome in ("deferred", "interrupted"):
                break
        self.res.metrics |= {"rungs_done": done, "spec_unmerged": sorted(unmerged)}
        return self.res

    def _pick(self, unmerged: set[str]) -> tuple[QueueEntry, str] | None:
        for e in load_queue(self.paths.queue).active():
            if e.id in unmerged:
                continue
            text = self._committed_spec(e.id)
            if text is None:
                unmerged.add(e.id)
                line = f"{e.id} skipped: spec_unmerged"
                if line not in self.res.lines:
                    self.res.lines.append(line)
                    log.info("backtest.runner.skipped", study=e.id, why="spec_unmerged")
                continue
            return e, text
        return None

    def _fail(self, entry: QueueEntry, rung: str | None, reason: str, detail: str) -> None:
        def go(e: QueueEntry) -> None:
            e.status = "failed"
            e.stop_reason = reason

        self._update(entry.id, go)
        log_path = self.paths.runs_dir / entry.id / _LOG_NAME
        where = f" on {rung}" if rung else ""
        self._emit(
            f"{entry.id} failed{where}: {reason}",
            f"BT · {entry.id} · failed{where}: {reason}{detail} · log {log_path}",
        )

    def _spec(self, entry: QueueEntry, text: str) -> StudySpec | None:
        try:
            spec = StudySpec.model_validate(yaml.safe_load(text) or {})
        except (ValueError, yaml.YAMLError) as exc:
            self._fail(entry, None, "committed spec does not load", f" ({str(exc)[:200]})")
            return None
        if study_hash(spec) != entry.spec_hash:
            self._fail(
                entry,
                None,
                f"spec hash on main {study_hash(spec)[:12]} != queued {entry.spec_hash[:12]}",
                "",
            )
            return None
        return spec

    def _advance(self, entry: QueueEntry, spec: StudySpec) -> tuple[str, str] | None:
        """The next rung to run (skipping dev rungs the spec's ladder lacks)."""
        state = entry.ladder_state
        while state is not None:
            run = rung_run(state, spec)
            if run is not None:
                if state != entry.ladder_state:
                    self._update(entry.id, lambda e, s=state: setattr(e, "ladder_state", s))
                return run
            i = RUNGS.index(state)
            state = RUNGS[i + 1] if i + 1 < len(RUNGS) else None
        self._update(entry.id, _concluded)
        return None

    def _run_rung(self, entry: QueueEntry, spec: StudySpec, phase: str, uni: str) -> str:
        rung = entry.ladder_state or "holdout"
        entries = load_ledger(self.paths.ledger)
        est = estimate_s(entries, entry.id, phase, uni, self.cfg.runtime_margin)
        start = self.clock().astimezone(ET).replace(microsecond=0)
        left = _elapsed(start, self.end).total_seconds()
        if est is not None and est > left:
            self.res.lines.append(
                f"{entry.id} {rung} deferred: window (needs ~{est / 60:.0f} min, "
                f"{left / 60:.0f} min left)"
            )
            return "deferred"
        study_dir = self.paths.runs_dir / entry.id
        spec_dir = study_dir / "spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / f"{entry.id}.yaml").write_text(self._committed_spec(entry.id) or "")
        log_path = study_dir / _LOG_NAME
        with log_path.open("a") as fh:
            fh.write(f"\n=== {start.isoformat()} {entry.id} {rung} ({phase} {uni}) ===\n")

        def running(e: QueueEntry) -> None:
            e.status = "running"

        got = self._update(entry.id, running)
        if got is None or got.status != "running":
            self.res.lines.append(f"{entry.id} {rung} not started: {got.status if got else 'gone'}")
            return "stopped"
        argv = rung_argv(
            entry.id,
            phase=phase,
            universe=uni,
            studies_dir=spec_dir,
            paths=self.paths,
            workers=self.cfg.workers,
            nice=self.cfg.nice,
            now=start,
            python=self.python,
        )
        rc, why = self._watch(self.launcher(argv, log_path, self.paths.cwd), entry.id)
        led = next(
            (
                e
                for e in load_ledger(self.paths.ledger)
                if e.study == entry.id
                and e.spec_hash == entry.spec_hash
                and e.phase == phase
                and e.at == start
            ),
            None,
        )
        if led is not None:
            return self._done(entry, spec, rung, led, start)
        run = QueueRun(rung=rung, at=start)  # type: ignore[arg-type]
        if why == "window":
            run.outcome = "interrupted"
            update_queue(self.paths.queue, lambda q: _apply(q, entry.id, run))
            self.res.lines.append(f"{entry.id} {rung} interrupted at the window end")
            return "interrupted"
        if why == "stopped":
            run.outcome = "stopped"
            update_queue(self.paths.queue, lambda q: _apply(q, entry.id, run))
            self.res.lines.append(f"{entry.id} {rung} stopped by the owner")
            return "stopped"
        run.outcome = "failed"
        update_queue(self.paths.queue, lambda q: _apply(q, entry.id, run))
        tail = _tail(log_path)
        last = next((x for x in reversed(tail) if x.strip()), "")
        self._fail(entry, rung, f"exit {rc}", f" ({last[:200]})" if last else "")
        return "failed"

    def _watch(self, h: RunHandle, study_id: str) -> tuple[int, str | None]:
        """Wait for *h*; SIGTERM at the window end or on an owner stop, SIGKILL after
        ``kill_grace``. Returns ``(exit code, why it was signalled | None)``."""
        why: str | None = None
        term_at: dt.datetime | None = None
        killed = False
        poll_s = self.cfg.poll.total_seconds()
        while (rc := h.poll()) is None:
            self.heartbeat()
            now = self.clock()
            if term_at is None:
                if now >= self.end:
                    why = "window"
                elif _stopped(self.paths.queue, study_id):
                    why = "stopped"
                if why is not None:
                    log.info("backtest.runner.sigterm", study=study_id, why=why, pid=h.pid)
                    h.terminate()
                    term_at = now
            elif not killed and _elapsed(term_at, now) >= self.cfg.kill_grace:
                log.warning("backtest.runner.sigkill", study=study_id, pid=h.pid)
                h.kill()
                killed = True
            self.sleep(poll_s)
        return rc, why

    def _done(
        self, entry: QueueEntry, spec: StudySpec, rung: str, led: LedgerEntry, start: dt.datetime
    ) -> str:
        key = f"{entry.id}:{led.spec_hash[:12]}:{led.at.isoformat()}"
        run = QueueRun(rung=rung, at=start, run_dir=led.run_dir, ledger_key=key, outcome="done")  # type: ignore[arg-type]
        nxt = _next_rung(rung, spec)
        if led.phase == "holdout":
            payload = _report(led)

            update_queue(self.paths.queue, lambda q: _apply(q, entry.id, run, _concluded))
            body = "\n".join(verdict_lines(payload)) if payload else rung_line(led)
            self._emit(
                f"{entry.id} holdout ({led.universe}) concluded",
                f"BT · {entry.id} · holdout verdict ({led.universe}):\n{body}",
            )
            return "done"
        if clear_dev_fail(led, self.cfg.fail_rule.ci_hi_below):
            reason = (
                f"dev fail on {rung}: every treatment a non-switch with CI upper bound < "
                f"{self.cfg.fail_rule.ci_hi_below:g} ({_fail_numbers(led)})"
            )

            def stop(e: QueueEntry) -> None:
                e.status = "stopped"
                e.ladder_state = None
                e.stop_reason = reason

            update_queue(self.paths.queue, lambda q: _apply(q, entry.id, run, stop))
            self._emit(
                f"{entry.id} stopped: dev fail on {rung}",
                f"BT · {entry.id} · stopped: dev fail on {rung} (no holdout spent) · "
                f"{rung_line(led)}",
            )
            return "done"

        def advance(e: QueueEntry) -> None:
            e.ladder_state = nxt  # type: ignore[assignment]

        update_queue(self.paths.queue, lambda q: _apply(q, entry.id, run, advance))
        self._emit(
            f"{entry.id} {rung} done",
            f"BT · {entry.id} · {rung} done ({led.universe}) · {rung_line(led)} · "
            f"next {nxt or '-'}",
        )
        return "done"


def _apply(
    q: Any, study_id: str, run: QueueRun, fn: Callable[[QueueEntry], None] | None = None
) -> None:
    """Append *run* to the entry (always: a finished rung is recorded even when the
    owner stopped the study meanwhile), then apply *fn* only while it is still active."""
    e = q.get(study_id)
    if e is None:
        return
    e.runs.append(run)
    if fn is not None and e.status in ("queued", "running"):
        fn(e)


def _concluded(e: QueueEntry) -> None:
    e.status = "concluded"
    e.ladder_state = None


def _next_rung(rung: str, spec: StudySpec) -> str | None:
    """The rung after *rung* that the spec's ladder has (``[c125]`` skips ``c500_dev``)."""
    for nxt in RUNGS[RUNGS.index(rung) + 1 :]:  # type: ignore[arg-type]
        if rung_run(nxt, spec) is not None:
            return nxt
    return None


def _stopped(queue: Path, study_id: str) -> bool:
    e = load_queue(queue).get(study_id)
    return e is None or e.status not in ("queued", "running")


def _report(led: LedgerEntry) -> dict[str, Any] | None:
    try:
        return json.loads((Path(led.run_dir) / "report.json").read_text())
    except (OSError, ValueError):
        return None


def queue_status(queue: Path) -> str:
    """One line for the Tower row: each active entry's id, status and next rung."""
    q = load_queue(queue)
    act = q.active()
    if not act:
        return "queue empty"
    return "queue: " + ", ".join(f"{e.id} {e.status} next {e.ladder_state or '-'}" for e in act)


def run_night(
    *,
    cfg: BacktestStudiesSettings,
    paths: RunnerPaths,
    clock: Callable[[], dt.datetime],
    sleep: Callable[[float], None],
    main: MainView,
    launcher: Callable[[Sequence[str], Path, Path], RunHandle] = popen_launcher,
    post: Callable[[str], None] = lambda _t: None,
    heartbeat: Callable[[], None] = lambda: None,
    topup_running: Callable[[], bool] = lambda: False,
    window_end: dt.datetime | None = None,
    python: str = sys.executable,
) -> NightResult:
    """Work the study queue until the window closes (see the module docstring).

    *window_end* overrides the computed end (the manual/live-evidence run); without it
    a *clock* outside the window raises :class:`SkipNightError`.
    """
    now = clock()
    idle = False
    if window_end is None:
        win = night_window(now, cfg)
        if win is None:
            msg = f"outside the study window ({cfg.window_start:%H:%M}-{cfg.window_end:%H:%M} ET)"
            raise SkipNightError(msg)
        window_end = win[1]
        # the slot runs on trading days only, so a night over a weekend/holiday is the
        # only run until the next session: it keeps polling the queue
        idle = window_end.date() - win[0].date() > dt.timedelta(days=1)
    waited = dt.timedelta(0)
    while topup_running():
        if waited >= cfg.topup_wait:
            msg = f"history.topup still running after {cfg.topup_wait}; skipped the night"
            raise SkipNightError(msg)
        sleep(cfg.poll.total_seconds())
        waited += cfg.poll
        heartbeat()
    night = _Night(
        cfg=cfg,
        paths=paths,
        clock=clock,
        sleep=sleep,
        main=main,
        launcher=launcher,
        post=post,
        heartbeat=heartbeat,
        end=window_end,
        python=python,
        idle=idle,
    )
    res = night.run()
    res.lines.append(queue_status(paths.queue))
    res.metrics |= {"window_end": window_end.isoformat(), "messages": len(res.messages)}
    return res
