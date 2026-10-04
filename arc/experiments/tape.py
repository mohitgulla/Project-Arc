"""Shared market inputs for paired arms: the control loop's market tape (E10.2, D44).

While an experiment arm pairs with control, every market-data read of a control
loop chain is recorded in the control store (``market_tape``, keyed by the
chain and the call). The arm's paired chain replays that chain's tape, so both
arms price, size and gate against the **identical** chain/quote snapshot; a call
the tape lacks (the arm asked for something control did not) falls through to
the live provider and is counted as a miss in the run's metrics/log.

Recording is on only while the control store has a ``running`` experiment and
``experiments.runner`` is enabled with at least one arm; otherwise the provider is
returned unwrapped (zero cost for a normal tick).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from arc.context.ttl import to_db
from arc.data.base import HistoryBar, OptionContract, UnderlyingQuote

if TYPE_CHECKING:
    from arc.data.base import MarketDataProvider

__all__ = [
    "TapeRecorder",
    "TapeReplay",
    "call_key",
    "prune_tape",
    "recording_enabled",
    "running_experiment",
    "tape_market",
]

log = structlog.get_logger(__name__)


def call_key(method: str, *args: Any) -> str:
    """Canonical ``method(args…)`` key of one provider call."""
    return f"{method}({json.dumps([str(a) for a in args])})"


def _pack(rows: list[Any]) -> bytes:
    return zlib.compress(
        json.dumps([r.model_dump(mode="json") for r in rows], default=str).encode()
    )


def _unpack(blob: bytes) -> list[dict[str, Any]]:
    return json.loads(zlib.decompress(blob).decode())


class TapeRecorder:
    """Wraps the control loop's provider and records every call under *chain_run_id*."""

    def __init__(
        self, inner: MarketDataProvider, conn: sqlite3.Connection, chain_run_id: str
    ) -> None:
        self.inner = inner
        self.conn = conn
        self.chain_run_id = chain_run_id

    def _put(self, key: str, rows: list[Any]) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT OR REPLACE INTO market_tape (chain_run_id, call, payload, at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        self.chain_run_id,
                        key,
                        _pack(rows),
                        to_db(_dt.datetime.now(_dt.UTC)),
                    ),
                )
        except sqlite3.Error as exc:  # recording is best effort; control never fails on it
            log.warning("experiments.tape_record_failed", call=key, error=str(exc))

    def option_chain(
        self, underlying: str, exp_start: _dt.date, exp_end: _dt.date
    ) -> list[OptionContract]:
        out = self.inner.option_chain(underlying, exp_start, exp_end)
        self._put(call_key("option_chain", underlying, exp_start, exp_end), list(out))
        return out

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        out = self.inner.underlying_quote(symbol)
        self._put(call_key("underlying_quote", symbol), [out])
        return out

    def history_bars(
        self, symbol: str, start: _dt.date, end: _dt.date, timeframe: str = "1Day"
    ) -> list[HistoryBar]:
        out = self.inner.history_bars(symbol, start, end, timeframe)
        self._put(call_key("history_bars", symbol, start, end, timeframe), list(out))
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class TapeReplay:
    """The arm's provider: control's recorded reads, live fallback on a miss."""

    def __init__(
        self,
        tape: dict[str, bytes],
        inner: MarketDataProvider | None,
        *,
        chain_run_id: str,
    ) -> None:
        self.tape = tape
        self.inner = inner
        self.chain_run_id = chain_run_id
        self.hits = 0
        self.misses = 0

    @classmethod
    def from_store(
        cls, control: sqlite3.Connection, chain_run_id: str, inner: MarketDataProvider | None
    ) -> TapeReplay:
        rows = control.execute(
            "SELECT call, payload FROM market_tape WHERE chain_run_id = ?", (chain_run_id,)
        ).fetchall()
        return cls({r[0]: bytes(r[1]) for r in rows}, inner, chain_run_id=chain_run_id)

    def _get(self, key: str) -> list[dict[str, Any]] | None:
        blob = self.tape.get(key)
        if blob is None:
            self.misses += 1
            log.warning("experiments.tape_miss", chain_run_id=self.chain_run_id, call=key)
            return None
        self.hits += 1
        return _unpack(blob)

    def _inner(self, key: str) -> MarketDataProvider:
        if self.inner is None:
            msg = f"market tape of {self.chain_run_id} has no {key} and no live fallback"
            raise LookupError(msg)
        return self.inner

    def option_chain(
        self, underlying: str, exp_start: _dt.date, exp_end: _dt.date
    ) -> list[OptionContract]:
        key = call_key("option_chain", underlying, exp_start, exp_end)
        got = self._get(key)
        if got is None:
            return self._inner(key).option_chain(underlying, exp_start, exp_end)
        return [OptionContract.model_validate(r) for r in got]

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        key = call_key("underlying_quote", symbol)
        got = self._get(key)
        if got is None:
            return self._inner(key).underlying_quote(symbol)
        return UnderlyingQuote.model_validate(got[0])

    def history_bars(
        self, symbol: str, start: _dt.date, end: _dt.date, timeframe: str = "1Day"
    ) -> list[HistoryBar]:
        key = call_key("history_bars", symbol, start, end, timeframe)
        got = self._get(key)
        if got is None:
            return self._inner(key).history_bars(symbol, start, end, timeframe)
        return [HistoryBar.model_validate(r) for r in got]

    def __getattr__(self, name: str) -> Any:
        if self.inner is None:
            raise AttributeError(name)
        return getattr(self.inner, name)


def running_experiment(conn: sqlite3.Connection) -> Any:
    """The control store's running experiment (``ExperimentState``) or ``None``."""
    from arc.experiments.models import ExperimentStatus
    from arc.experiments.store import ExperimentStore

    try:
        running = ExperimentStore(conn).all(status=ExperimentStatus.RUNNING)
    except sqlite3.OperationalError:
        return None
    return running[0] if running else None


def recording_enabled(conn: sqlite3.Connection) -> bool:
    """A control store records its loop's market reads only while an arm may pair."""
    if running_experiment(conn) is None:
        return False
    from arc.control.effective import effective_settings, experiments_config

    runner = experiments_config(effective_settings(conn)).runner
    return runner.enabled and bool(runner.arms)


def tape_market(
    conn: sqlite3.Connection | None,
    chain_run_id: str | None,
    inner: MarketDataProvider,
) -> MarketDataProvider:
    """The provider a loop step uses on *conn* (see the module doc)."""
    if conn is None or not chain_run_id:
        return inner
    from arc.experiments.arms import read_identity

    ident = read_identity(conn)
    if ident is not None:
        pair = conn.execute(
            "SELECT control_chain_run_id FROM arm_pairs WHERE arm_chain_run_id = ?",
            (chain_run_id,),
        ).fetchone()
        if pair is None or not Path(ident.control_db).is_file():
            return inner
        ctl = sqlite3.connect(f"file:{ident.control_db}?mode=ro", uri=True)
        try:
            return TapeReplay.from_store(ctl, pair[0], inner)
        finally:
            ctl.close()
    if recording_enabled(conn):
        return TapeRecorder(inner, conn, chain_run_id)
    return inner


def prune_tape(conn: sqlite3.Connection, *, keep_days: int, now: _dt.datetime) -> int:
    """Drop tape rows older than *keep_days* (a cache, not an audit table)."""
    cutoff = to_db(now - _dt.timedelta(days=keep_days))
    with conn:
        cur = conn.execute("DELETE FROM market_tape WHERE at < ?", (cutoff,))
    return cur.rowcount
