"""E7.9 (D84): ThetaData pull progress, checkpoint completion and the option-liquidity score.

Three pure-ish pieces around the one-month Value-tier pull of an E7.7 universe:

* **Progress** (:func:`pull_status`): per checkpoint (``c125 ⊂ c250 ⊂ c500``), how many
  (name, session) files are cached under ``options_eod/provider=<p>/``, GB on disk, and
  an ETA from a measured sessions/hour rate. Counting is by file name only (cheap
  enough to run every 30 min over ~850k files); OI completeness is verified from the
  parquet footers once, when a checkpoint first reaches 100 % (:func:`verify_oi`).
* **checkpoints.json** (``<data-dir>/backtest_universe/checkpoints.json``,
  ``{c125: {complete_at, sessions, gb, rows, names, version}, ...}``): written when a
  checkpoint is complete (every name has quotes **and** OI for every session).
  :func:`require_checkpoint` refuses a backtest on an incomplete checkpoint of the
  pulled provider; the E9 Analyst reads the same file.
* **Option-liquidity score** (:func:`optliq_scores`, the owner's 2026-10-11 replacement
  for E7.7's Stage 2 probe): per (quarter start Q, symbol) in the universe membership,
  near-ATM open interest (spot ± ``strike_range`` strikes, ≤ ``max_dte``) on the last
  session strictly before Q, plus total option volume over the ``volume_days``
  sessions ending there; ``score = oi_weight × oi + volume_weight × volume`` with
  :class:`arc.backtest.universe.Stage2Config`'s weights. Point-in-time: only sessions
  < Q are read. Written as ``<version>.optliq.parquet``; it is an eligibility /
  liquidity input, never a re-ranking of the pull list.

No network, no LLM, no audit-DB access. Aggregates only (ThetaData retail terms).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow.parquet as pq
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.universe import universe_dir
from arc.data.history.store import OI_META_KEY, ParquetHistoryStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from arc.backtest.universe import Stage2Config, UniverseRow

log = structlog.get_logger()

__all__ = [
    "CHECKPOINTS_FILE",
    "CheckpointIncompleteError",
    "CheckpointRecord",
    "CheckpointStatus",
    "OptLiqRow",
    "PullStatus",
    "checkpoint_sizes",
    "cut_line",
    "format_status",
    "measured_rate",
    "optliq_path",
    "optliq_scores",
    "optliq_summary",
    "progress_path",
    "pull_status",
    "read_checkpoints",
    "record_snapshot",
    "require_checkpoint",
    "verify_oi",
    "would_move",
    "write_checkpoint",
    "write_optliq",
]

CHECKPOINTS_FILE = "checkpoints.json"
_GB = 1e9


class CheckpointIncompleteError(RuntimeError):
    """A backtest asked for a checkpoint the pull has not completed."""


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


class CheckpointStatus(BaseModel):
    """Cached (name, session) files for one checkpoint (nested: c250 includes c125)."""

    model_config = ConfigDict(extra="forbid")

    checkpoint: str
    names: int
    names_complete: int = Field(..., description="Names with every session cached")
    sessions_total: int = Field(..., description="names × sessions in the window")
    sessions_done: int
    bytes: int

    @property
    def complete(self) -> bool:
        return self.sessions_done >= self.sessions_total

    @property
    def remaining(self) -> int:
        return max(0, self.sessions_total - self.sessions_done)

    @property
    def gb(self) -> float:
        return self.bytes / _GB


class PullStatus(BaseModel):
    """Snapshot of the pull across every checkpoint plus per-name cached counts."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    version: str
    start: dt.date
    end: dt.date
    sessions: int
    checkpoints: list[CheckpointStatus]
    done_by_name: dict[str, int] = Field(default_factory=dict)
    order: list[str] = Field(default_factory=list, description="Pull-list priority order")

    def checkpoint(self, name: str) -> CheckpointStatus:
        for c in self.checkpoints:
            if c.checkpoint == name:
                return c
        msg = f"no checkpoint {name}"
        raise KeyError(msg)

    @property
    def current(self) -> str | None:
        """First checkpoint not yet complete (None once all are)."""
        return next((c.checkpoint for c in self.checkpoints if not c.complete), None)


def checkpoint_sizes(rows: Iterable[UniverseRow]) -> dict[str, int]:
    """``{"c125": 125, ...}`` from the rows' own checkpoint labels (nested sizes)."""
    out: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: r.position):
        out[r.checkpoint] = r.position
    return out


def _scan(directory: Path, window: frozenset[str]) -> tuple[int, int]:
    """(session files in *window*, their bytes) under one underlying directory."""
    n = size = 0
    try:
        it = os.scandir(directory)
    except FileNotFoundError:
        return 0, 0
    with it:
        for e in it:
            if e.name.endswith(".parquet") and e.name[:-8] in window:
                n += 1
                size += e.stat().st_size
    return n, size


def pull_status(
    data_dir: Path,
    rows: Sequence[UniverseRow],
    *,
    version: str,
    provider: str,
    sessions: Sequence[dt.date],
) -> PullStatus:
    """Count cached session files per name and roll them up per nested checkpoint."""
    store = ParquetHistoryStore(data_dir)
    window = frozenset(d.isoformat() for d in sessions)
    ordered = sorted(rows, key=lambda r: r.position)
    done: dict[str, int] = {}
    size: dict[str, int] = {}
    for r in ordered:
        n, b = _scan(store.path_for(provider, r.symbol, sessions[0]).parent, window)
        done[r.symbol], size[r.symbol] = n, b
    total = len(sessions)
    cps: list[CheckpointStatus] = []
    for cp, n_names in checkpoint_sizes(ordered).items():
        names = [r.symbol for r in ordered[:n_names]]
        cps.append(
            CheckpointStatus(
                checkpoint=cp,
                names=len(names),
                names_complete=sum(done[s] >= total for s in names),
                sessions_total=len(names) * total,
                sessions_done=sum(min(done[s], total) for s in names),
                bytes=sum(size[s] for s in names),
            )
        )
    return PullStatus(
        provider=provider,
        version=version,
        start=sessions[0],
        end=sessions[-1],
        sessions=total,
        checkpoints=cps,
        done_by_name=done,
        order=[r.symbol for r in ordered],
    )


def verify_oi(
    data_dir: Path,
    symbols: Sequence[str],
    *,
    provider: str,
    sessions: Sequence[dt.date],
) -> tuple[list[tuple[str, dt.date]], int]:
    """Session files missing or lacking the OI stamp, plus total rows (parquet footers)."""
    store = ParquetHistoryStore(data_dir)
    bad: list[tuple[str, dt.date]] = []
    rows = 0
    for s in symbols:
        for d in sessions:
            p = store.path_for(provider, s, d)
            if not p.is_file():
                bad.append((s, d))
                continue
            meta = pq.read_metadata(p)
            rows += meta.num_rows
            kv = meta.metadata or {}
            if kv.get(OI_META_KEY) != b"1":
                bad.append((s, d))
    return bad, rows


def cut_line(status: PullStatus, sessions_per_hour: float, hours_left: float) -> int:
    """Pull-list position the pull reaches in *hours_left* at the measured rate.

    Names are pulled front-first, so the projection walks the priority order spending
    the session budget on each name's remaining sessions. Returns the last position
    that would be complete (``len(order)`` when everything fits).
    """
    budget = max(0.0, sessions_per_hour * hours_left)
    pos = 0
    for s in status.order:
        need = max(0, status.sessions - status.done_by_name.get(s, 0))
        if need > budget:
            break
        budget -= need
        pos += 1
    return pos


def _fmt_eta(hours: float) -> str:
    if not math.isfinite(hours):
        return "?"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.1f} d"


def format_status(
    status: PullStatus,
    *,
    now: dt.datetime,
    sessions_per_hour: float | None,
    deadline: dt.date | None = None,
    buffer_days: int = 7,
) -> str:
    """The progress post: names done / total, current checkpoint, GB, rate, ETA per checkpoint."""
    last = status.checkpoints[-1]
    total_names = last.names
    names_done = last.names_complete
    lines = [
        f"E7.9 · ThetaData pull · {now:%a %b %-d %H:%M %Z}",
        f"names complete {names_done}/{total_names} · current checkpoint "
        f"{status.current or 'all complete'} · {last.gb:.1f} GB on disk · "
        f"sessions {last.sessions_done:,}/{last.sessions_total:,}",
    ]
    rate = sessions_per_hour or 0.0
    if rate > 0:
        lines.append(f"rate {rate:,.0f} name-sessions/h ({rate * 24:,.0f}/day)")
    for c in status.checkpoints:
        if c.complete:
            lines.append(f"• {c.checkpoint}: complete · {c.gb:.1f} GB")
            continue
        pct = 100.0 * c.sessions_done / max(1, c.sessions_total)
        hours = c.remaining / rate if rate > 0 else float("inf")
        when = f" (≈ {(now + dt.timedelta(hours=hours)):%a %b %-d})" if math.isfinite(hours) else ""
        lines.append(
            f"• {c.checkpoint}: {pct:.1f}% · {c.names_complete}/{c.names} names · "
            f"{c.gb:.1f} GB · ETA {_fmt_eta(hours)}{when}"
        )
    if deadline is not None and rate > 0 and not last.complete:
        target = deadline - dt.timedelta(days=buffer_days)
        hours_left = (
            dt.datetime.combine(target, dt.time(0), tzinfo=now.tzinfo) - now
        ).total_seconds() / 3600
        finish = now + dt.timedelta(hours=last.remaining / rate)
        if finish.date() > target:
            pos = cut_line(status, rate, hours_left)
            lines.append(
                f"⚠️ behind: at this rate {last.checkpoint} finishes ≈ {finish:%a %b %-d}, after "
                f"{target:%b %-d} (cancel {deadline:%b %-d} − {buffer_days} d buffer). "
                f"Projected cut line: position {pos} "
                f"({status.order[pos - 1] if pos else 'none'})."
            )
    return "\n".join(lines)


def progress_path(data_dir: Path, version: str) -> Path:
    return universe_dir(data_dir) / f"{version}.pull_progress.jsonl"


def record_snapshot(data_dir: Path, status: PullStatus, now: dt.datetime) -> Path:
    """Append ``{at, sessions_done, gb}`` (the whole list) for the rate measurement."""
    last = status.checkpoints[-1]
    p = progress_path(data_dir, status.version)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": now.isoformat(), "sessions_done": last.sessions_done, "gb": round(last.gb, 3)}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return p


def measured_rate(
    data_dir: Path,
    version: str,
    now: dt.datetime,
    sessions_done: int,
    *,
    window_hours: float = 24.0,
    min_hours: float = 0.5,
) -> float | None:
    """Name-sessions per hour since the oldest snapshot inside *window_hours* (None if too new)."""
    p = progress_path(data_dir, version)
    if not p.is_file():
        return None
    lo = now - dt.timedelta(hours=window_hours)
    base: tuple[dt.datetime, int] | None = None
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
            at = dt.datetime.fromisoformat(rec["at"])
        except (ValueError, KeyError):
            continue
        if at >= lo:
            base = (at, int(rec["sessions_done"]))
            break
    if base is None:
        return None
    hours = (now - base[0]).total_seconds() / 3600
    if hours < min_hours:
        return None
    return max(0.0, (sessions_done - base[1]) / hours)


# ---------------------------------------------------------------------------
# checkpoints.json
# ---------------------------------------------------------------------------


class CheckpointRecord(BaseModel):
    """One entry of ``checkpoints.json``."""

    model_config = ConfigDict(extra="allow")

    complete_at: dt.datetime | None = None
    sessions: int
    gb: float
    rows: int | None = None
    names: int | None = None
    version: str | None = None
    provider: str = "thetadata"
    start: dt.date | None = None
    end: dt.date | None = None


def checkpoints_path(data_dir: Path) -> Path:
    return universe_dir(data_dir) / CHECKPOINTS_FILE


def read_checkpoints(data_dir: Path) -> dict[str, CheckpointRecord]:
    p = checkpoints_path(data_dir)
    if not p.is_file():
        return {}
    raw = json.loads(p.read_text())
    return {k: CheckpointRecord.model_validate(v) for k, v in raw.items()}


def write_checkpoint(data_dir: Path, name: str, record: CheckpointRecord) -> Path:
    """Add/replace one checkpoint entry (atomic). A recorded ``complete_at`` is kept."""
    cur = read_checkpoints(data_dir)
    old = cur.get(name)
    if old is not None and old.complete_at is not None and record.complete_at is not None:
        record = record.model_copy(update={"complete_at": old.complete_at})
    cur[name] = record
    p = checkpoints_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({k: v.model_dump(mode="json") for k, v in sorted(cur.items())}, indent=2) + "\n"
    )
    tmp.replace(p)
    return p


def require_checkpoint(data_dir: Path, universe: str, *, provider: str, version: str) -> None:
    """Refuse a backtest on a checkpoint the *provider* pull has not completed.

    Only enforced once ``checkpoints.json`` exists (it is written by the E7.9 pull):
    before that, studies run on whatever partitions exist and their report header
    names them (E7.10 fallback). A record for another provider or universe version
    does not vouch for this one.
    """
    recs = read_checkpoints(data_dir)
    if not recs:
        return
    rec = recs.get(universe.lower())
    if rec is None or rec.complete_at is None:
        done = ", ".join(sorted(k for k, v in recs.items() if v.complete_at)) or "none"
        msg = (
            f"checkpoint {universe} is not complete in {checkpoints_path(data_dir)} "
            f"(complete: {done}); the E7.9 pull is still filling it"
        )
        raise CheckpointIncompleteError(msg)
    if rec.provider == provider and rec.version is not None and rec.version != version:
        msg = (
            f"checkpoint {universe} was completed for universe {rec.version}, "
            f"not {version}; pass that universe_version"
        )
        raise CheckpointIncompleteError(msg)


# ---------------------------------------------------------------------------
# option-liquidity score (post-pull Stage 2 replacement)
# ---------------------------------------------------------------------------


class OptLiqRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quarter: dt.date
    symbol: str
    near_atm_oi: float
    opt_volume_20d: float
    score: float
    sessions_used: int


_COLS = ["date", "expiration", "strike", "volume", "open_interest"]


def _read_day(store: ParquetHistoryStore, provider: str, symbol: str, d: dt.date) -> pd.DataFrame:
    p = store.path_for(provider, symbol, d)
    if not p.is_file():
        return pd.DataFrame(columns=_COLS)
    return pq.read_table(p, columns=_COLS).to_pandas()


def near_atm_oi(day: pd.DataFrame, spot: float, *, d: dt.date, cfg: Stage2Config) -> float:
    """OI over the ``strike_range`` strikes each side of *spot* (+ the ATM strike), ≤ max_dte.

    Matches Theta's ``strike_range`` semantics used by the Stage 2 probe: per
    expiration, the ATM strike (nearest to spot) and ``strike_range`` listed strikes
    above and below it, both rights.
    """
    if day.empty or not math.isfinite(spot) or spot <= 0:
        return 0.0
    exp = pd.to_datetime(day["expiration"]).dt.date
    dte = (pd.to_datetime(exp) - pd.Timestamp(d)).dt.days
    ok = (dte >= 0) & (dte <= cfg.max_dte)
    df = day.loc[ok, ["expiration", "strike", "open_interest"]]
    total = 0.0
    for _, g in df.groupby("expiration"):
        strikes = sorted(set(g["strike"].astype(float)))
        if not strikes:
            continue
        i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot))
        lo, hi = max(0, i - cfg.strike_range), min(len(strikes), i + cfg.strike_range + 1)
        keep = set(strikes[lo:hi])
        total += float(
            pd.to_numeric(g.loc[g["strike"].isin(keep), "open_interest"], errors="coerce")
            .fillna(0)
            .sum()
        )
    return total


def optliq_scores(
    data_dir: Path,
    membership: pd.DataFrame,
    *,
    provider: str,
    cfg: Stage2Config,
    sessions: Sequence[dt.date],
    spot: Callable[[str, dt.date], float | None],
    symbols: Iterable[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[OptLiqRow]:
    """Score every (quarter, symbol) of *membership* from the cached parquet only.

    *sessions* is the trading calendar; *spot* returns the raw close for (symbol, d).
    Only sessions strictly before each quarter start are read (point-in-time).
    *symbols* restricts the rows (e.g. one checkpoint). Uncached sessions count as 0
    and are reflected in ``sessions_used``.
    """
    store = ParquetHistoryStore(data_dir)
    m = membership[["quarter", "symbol"]].copy()
    m["quarter"] = pd.to_datetime(m["quarter"]).dt.date
    if symbols is not None:
        keep = {s.upper() for s in symbols}
        m = m[m["symbol"].isin(keep)]
    by_symbol: dict[str, list[dt.date]] = {}
    for q, s in sorted({(q, s) for q, s in m.itertuples(index=False)}):
        by_symbol.setdefault(s, []).append(q)
    cal = sorted(sessions)
    out: list[OptLiqRow] = []
    for k, (s, quarters) in enumerate(sorted(by_symbol.items()), 1):
        vol_by_day: dict[dt.date, float] = {}  # each day file read once per symbol
        for q in quarters:
            window = [d for d in cal if d < q][-cfg.volume_days :]
            volume = 0.0
            used = 0
            for d in window:
                if d not in vol_by_day:
                    day = _read_day(store, provider, s, d)
                    vol_by_day[d] = float(
                        pd.to_numeric(day["volume"], errors="coerce").fillna(0).sum()
                    )
                volume += vol_by_day[d]
                used += store.has(provider, s, d)
            oi = 0.0
            if window:
                d = window[-1]
                px = spot(s, d)
                if px is not None:
                    oi = near_atm_oi(_read_day(store, provider, s, d), float(px), d=d, cfg=cfg)
            out.append(
                OptLiqRow(
                    quarter=q,
                    symbol=s,
                    near_atm_oi=oi,
                    opt_volume_20d=volume,
                    score=cfg.oi_weight * oi + cfg.volume_weight * volume,
                    sessions_used=used,
                )
            )
        if progress is not None:
            progress(k, len(by_symbol))
    return sorted(out, key=lambda r: (r.quarter, r.symbol))


def optliq_path(data_dir: Path, version: str) -> Path:
    return universe_dir(data_dir) / f"{version}.optliq.parquet"


def write_optliq(data_dir: Path, version: str, rows: Sequence[OptLiqRow]) -> Path:
    """Write (or merge into) the sidecar; rows replace earlier ones for the same key."""
    p = optliq_path(data_dir, version)
    new = pd.DataFrame([r.model_dump() for r in rows], columns=list(OptLiqRow.model_fields))
    if p.is_file():
        old = pd.read_parquet(p)
        old["quarter"] = pd.to_datetime(old["quarter"]).dt.date
        new = pd.concat([old, new]).drop_duplicates(["quarter", "symbol"], keep="last")
    new = new.sort_values(["quarter", "symbol"]).reset_index(drop=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".parquet.tmp")
    new.to_parquet(tmp, index=False)
    tmp.replace(p)
    return p


def optliq_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Per quarter: rows, % symbols with score > 0, median / p10 / p90 score."""
    if df.empty:
        return pd.DataFrame(columns=["quarter", "rows", "pct_scored", "p10", "median", "p90"])
    g = df.groupby("quarter")["score"]
    out = pd.DataFrame(
        {
            "rows": g.size(),
            "pct_scored": g.apply(lambda s: 100.0 * float((s > 0).mean())),
            "p10": g.quantile(0.10),
            "median": g.median(),
            "p90": g.quantile(0.90),
        }
    ).reset_index()
    return out


def would_move(
    df: pd.DataFrame, membership: pd.DataFrame, top: int
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Names a Stage-2 (score) ordering would have moved in / out of each quarter's top *top*.

    Report only. Compares, per quarter, the stage-1 ``in_top`` set against the *top*
    names by score among the scored rows; returns ``(moved_in, moved_out)`` as
    (symbol, quarters) counts, sorted by count.
    """
    m = membership.copy()
    m["quarter"] = pd.to_datetime(m["quarter"]).dt.date
    d = df.copy()
    d["quarter"] = pd.to_datetime(d["quarter"]).dt.date
    ins: dict[str, int] = {}
    outs: dict[str, int] = {}
    for q, g in d.groupby("quarter"):
        s1 = set(m.loc[(m["quarter"] == q) & m["in_top"].astype(bool), "symbol"])
        s1 &= set(g["symbol"])
        s2 = set(g.sort_values("score", ascending=False)["symbol"].head(len(s1) or top))
        for s in s2 - s1:
            ins[s] = ins.get(s, 0) + 1
        for s in s1 - s2:
            outs[s] = outs.get(s, 0) + 1

    def _sorted(x: Mapping[str, int]) -> list[tuple[str, int]]:
        return sorted(x.items(), key=lambda kv: (-kv[1], kv[0]))

    return _sorted(ins), _sorted(outs)
