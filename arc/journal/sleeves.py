"""Per-sleeve closed-trade report and the D97 keep/shrink rule (E22.5).

Read-only and deterministic (no LLM, no network, no broker). Report only: nothing here
reads or writes config; the owner applies a verdict by hand (registry or PR).

**Sleeve of a trade** is always *derived* from its entry DTE with
:func:`arc.account_profiles.sleeve_for_dte` against the sleeves passed in (the active
profile's :attr:`~arc.config.ArcSettings.entry_sleeves`), never read from the stored
``Structure.sleeve``. So a pre-D97 30–60 DTE trade maps to ``swing``, and a trade whose
entry DTE fits no current sleeve is :data:`OTHER`.

**Per sleeve:** opens in the window, closed trades (the scorecard's
:func:`~arc.journal.scorecard.closed_positions`), win rate, mean / median realised P&L
(fills only, before fees), mean net P&L (realised − cost-model commission and fees of
the open and close fills), mean P&L as a share of max loss (realised ÷ (structure max
loss × contracts)), mean days held, exit-reason mix, entry slippage (fill − mid beyond
nothing, ``execution_costs``), and mean max adverse / favourable excursion (the
``outcomes`` row of the open, where recorded).

**Keep/shrink rule (D97, fixed before any result):** a sleeve is *ready* once it has
≥ :data:`MIN_CLOSED` closed trades or :data:`MAX_WEEKS` weeks have passed since the
measurement start (the D80 ``store:epoch``, else the first open), whichever is first.
A ready sleeve is ``keep`` if its mean net P&L ≥ 0; ``shrink`` (to
:data:`SHRINK_SLOTS` slots) if its mean is < 0 **and** the bootstrap
:data:`CI_LEVEL` CI of the mean lies entirely below 0; else ``collect``. A sleeve that is
not ready is ``collect``. ``other`` gets no verdict.
"""

from __future__ import annotations

import datetime as _dt
import json
import random
import sqlite3
import statistics
from collections import Counter
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.account_profiles import DEFAULT_SLEEVE, entry_dte_of, sleeve_for_dte
from arc.journal.scorecard import _ts, closed_positions, execution_costs
from arc.models import Structure
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from arc.account_profiles import DteSleeve
    from arc.journal.scorecard import ClosedPosition

__all__ = [
    "CI_LEVEL",
    "MAX_WEEKS",
    "MIN_CLOSED",
    "OTHER",
    "SHRINK_SLOTS",
    "SleeveReport",
    "SleeveStats",
    "bootstrap_mean_ci",
    "day_sleeve_lines",
    "entry_dtes",
    "measurement_start",
    "sleeve_name",
    "sleeve_report",
    "sleeve_report_lines",
    "sleeves_by_structure",
    "verdict",
]

#: Name of the bucket for an entry DTE that fits no current sleeve.
OTHER = "other"
#: D97 keep/shrink rule constants (fixed in advance; changing them is an owner decision).
MIN_CLOSED = 30
MAX_WEEKS = 6
CI_LEVEL = 0.90
SHRINK_SLOTS = 2
BOOTSTRAP_RESAMPLES = 5_000
BOOTSTRAP_SEED = 97

Verdict = Literal["keep", "shrink", "collect"]

_FORBID = ConfigDict(extra="forbid", frozen=True)
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
_FOREVER = _dt.datetime(9999, 1, 1, tzinfo=_dt.UTC)
_DAY_S = 86_400.0


def sleeve_name(sleeves: Sequence[DteSleeve], dte: int | None) -> str:
    """The sleeve containing entry DTE *dte*, else :data:`OTHER` (``None`` DTE too)."""
    s = sleeve_for_dte(sleeves, dte)
    return s.name if s is not None else OTHER


def entry_dtes(conn: sqlite3.Connection) -> dict[str, int]:
    """``open_structures.id`` -> entry DTE, for every structure the store holds."""
    out: dict[str, int] = {}
    for r in conn.execute("SELECT id, structure_json, opened_at FROM open_structures"):
        try:
            st = Structure.model_validate_json(r[1])
            out[str(r[0])] = entry_dte_of(st, r[2])
        except ValueError:
            continue
    return out


def sleeves_by_structure(conn: sqlite3.Connection, sleeves: Sequence[DteSleeve]) -> dict[str, str]:
    """``open_structures.id`` -> derived sleeve name (:func:`sleeve_name` of its entry DTE)."""
    return {sid: sleeve_name(sleeves, dte) for sid, dte in entry_dtes(conn).items()}


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    level: float = CI_LEVEL,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean of *values*; ``None`` when empty.

    Deterministic: a private :class:`random.Random` seeded with *seed* (no global state).
    """
    n = len(values)
    if n == 0:
        return None
    if n == 1:
        return float(values[0]), float(values[0])
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    tail = (1.0 - level) / 2.0
    lo = means[int(tail * (resamples - 1))]
    hi = means[int(round((1.0 - tail) * (resamples - 1)))]
    return float(lo), float(hi)


def verdict(
    n: int, mean: float | None, ci: tuple[float, float] | None, *, weeks: float | None
) -> tuple[bool, Verdict]:
    """``(ready, verdict)`` of one sleeve under the D97 rule (see the module doc)."""
    ready = n >= MIN_CLOSED or (weeks is not None and weeks >= MAX_WEEKS)
    if not ready or mean is None:
        return ready, "collect"
    if mean >= 0:
        return ready, "keep"
    if ci is not None and ci[1] < 0:
        return ready, "shrink"
    return ready, "collect"


def measurement_start(conn: sqlite3.Connection) -> _dt.datetime | None:
    """The D80 ``store:epoch`` start, else the first open; ``None`` for an empty store."""
    try:
        row = conn.execute("SELECT value FROM routine_state WHERE key = 'store:epoch'").fetchone()
    except sqlite3.OperationalError:  # an older store without routine_state
        row = None
    if row is not None:
        try:
            started = _ts(json.loads(row[0]).get("started_at"))
        except (ValueError, AttributeError, TypeError):
            started = None
        if started is not None:
            return started
    first = conn.execute("SELECT MIN(opened_at) FROM open_structures").fetchone()
    return _ts(first[0]) if first and first[0] else None


class SleeveStats(BaseModel):
    """One sleeve's closed-trade statistics over the window."""

    model_config = _FORBID

    sleeve: str
    dte_min: int | None = Field(None, description="None for `other`")
    dte_max: int | None = None
    max_positions: int | None = Field(None, description="Slot cap (None = uncapped / other)")
    opens: int = Field(0, description="Positions opened in the window")
    closed: int = 0
    wins: int = 0
    win_rate: float | None = None
    realised: float = Field(0.0, description="Σ realised P&L, fills only, before fees")
    mean_pnl: float | None = None
    median_pnl: float | None = None
    costs: float = Field(0.0, description="Σ cost-model commission + fees of the trades' fills")
    mean_net_pnl: float | None = Field(None, description="Mean of realised − costs (the rule's)")
    ci_low: float | None = Field(None, description=f"Bootstrap {CI_LEVEL:.0%} CI of mean net")
    ci_high: float | None = None
    profit_factor: float | None = Field(
        None, description="Σ winning ÷ |Σ losing| realised P&L; None with no losing dollars"
    )
    mean_pct_of_max_loss: float | None = Field(
        None, description="Mean realised ÷ (max loss × contracts), over trades with a max loss"
    )
    mean_days_held: float | None = None
    exit_reasons: dict[str, int] = Field(default_factory=dict)
    entry_slippage: float | None = Field(None, description="Σ entry fill − mid, $ (+ = worse)")
    mean_entry_slippage: float | None = None
    mean_mae: float | None = Field(None, description="Mean max adverse excursion, $ (≤ 0)")
    mean_mfe: float | None = Field(None, description="Mean max favourable excursion, $ (≥ 0)")
    n_excursion: int = 0
    ready: bool = False
    verdict: Verdict | None = Field(None, description="None for `other`")

    @property
    def window(self) -> str | None:
        """``10–29`` or ``None`` for ``other``."""
        if self.dte_min is None or self.dte_max is None:
            return None
        return f"{self.dte_min}–{self.dte_max}"


class SleeveReport(BaseModel):
    model_config = _FORBID

    since: _dt.datetime | None = None
    until: _dt.datetime
    measurement_start: _dt.datetime | None = None
    weeks: float | None = Field(None, description="Weeks from measurement start to until")
    min_closed: int = MIN_CLOSED
    max_weeks: int = MAX_WEEKS
    ci_level: float = CI_LEVEL
    shrink_slots: int = SHRINK_SLOTS
    rule: str = Field(
        default=(
            f"Read after >= {MIN_CLOSED} closed trades per sleeve or {MAX_WEEKS} weeks, "
            "whichever first. Keep a sleeve if its mean net P&L >= 0; shrink it to "
            f"{SHRINK_SLOTS} slots if its mean < 0 and the bootstrap {CI_LEVEL:.0%} CI of "
            "the mean is entirely below 0; otherwise keep collecting. Report only."
        )
    )
    sleeves: list[SleeveStats] = Field(default_factory=list)


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _max_loss(conn: sqlite3.Connection, sid: str) -> float | None:
    row = conn.execute(
        "SELECT json_extract(structure_json, '$.max_loss') FROM open_structures WHERE id = ?",
        (sid,),
    ).fetchone()
    try:
        return float(row[0]) if row and row[0] is not None else None
    except (TypeError, ValueError):
        return None


def _excursions(conn: sqlite3.Connection, phash: str) -> tuple[float | None, float | None]:
    """(MAE, MFE) of the latest ``outcomes`` row of the open proposal, where recorded."""
    row = conn.execute(
        """SELECT max_adverse_excursion, max_favourable_excursion FROM outcomes
           WHERE proposal_hash = ? ORDER BY at DESC, rowid DESC LIMIT 1""",
        (phash,),
    ).fetchone()
    if row is None:
        return None, None

    def f(v: object) -> float | None:
        try:
            return None if v is None else float(str(v))
        except ValueError:
            return None

    return f(row[0]), f(row[1])


def _stats(  # noqa: PLR0913 - one sleeve's joins
    conn: sqlite3.Connection,
    name: str,
    sleeve: DteSleeve | None,
    trades: list[ClosedPosition],
    opens: int,
    costs_by_sid: dict[str, float],
    slip_by_sid: dict[str, float],
    weeks: float | None,
) -> SleeveStats:
    pnl = [c.realised_pnl for c in trades]
    net = [c.realised_pnl - costs_by_sid.get(c.structure_id, 0.0) for c in trades]
    pct: list[float] = []
    for c in trades:
        ml = _max_loss(conn, c.structure_id)
        if ml and ml > 0 and c.contracts > 0:
            pct.append(c.realised_pnl / (ml * c.contracts))
    held = [
        (c.closed_at - c.opened_at).total_seconds() / _DAY_S
        for c in trades
        if c.opened_at is not None
    ]
    slips = [slip_by_sid[c.structure_id] for c in trades if c.structure_id in slip_by_sid]
    mae: list[float] = []
    mfe: list[float] = []
    for c in trades:
        a, b = _excursions(conn, c.open_proposal_hash)
        if a is not None:
            mae.append(a)
        if b is not None:
            mfe.append(b)
    mean_net = _mean(net)
    ci = bootstrap_mean_ci(net)
    ready, v = verdict(len(trades), mean_net, ci, weeks=weeks)
    wins = sum(1 for x in pnl if x > 0)
    gross_w = sum(x for x in pnl if x > 0)
    gross_l = sum(x for x in pnl if x <= 0)
    return SleeveStats(
        sleeve=name,
        dte_min=sleeve.dte_min if sleeve else None,
        dte_max=sleeve.dte_max if sleeve else None,
        max_positions=sleeve.max_positions if sleeve else None,
        opens=opens,
        closed=len(trades),
        wins=wins,
        win_rate=wins / len(trades) if trades else None,
        realised=sum(pnl),
        mean_pnl=_mean(pnl),
        median_pnl=float(statistics.median(pnl)) if pnl else None,
        costs=sum(costs_by_sid.get(c.structure_id, 0.0) for c in trades),
        mean_net_pnl=mean_net,
        ci_low=ci[0] if ci else None,
        ci_high=ci[1] if ci else None,
        profit_factor=gross_w / -gross_l if gross_l < 0 else None,
        mean_pct_of_max_loss=_mean(pct),
        mean_days_held=_mean(held),
        exit_reasons=dict(sorted(Counter(c.exit_reason for c in trades).items())),
        entry_slippage=sum(slips) if slips else None,
        mean_entry_slippage=_mean(slips),
        mean_mae=_mean(mae),
        mean_mfe=_mean(mfe),
        n_excursion=len(mae),
        ready=ready if sleeve else False,
        verdict=v if sleeve else None,
    )


def sleeve_report(  # noqa: PLR0913 - window + optional pre-filtered rows
    conn: sqlite3.Connection,
    sleeves: Sequence[DteSleeve],
    *,
    since: _dt.datetime | None,
    until: _dt.datetime,
    now: _dt.datetime,
    closed: Iterable[ClosedPosition] | None = None,
    exclude_hashes: Iterable[str] = (),
) -> SleeveReport:
    """Per-sleeve stats of trades closed (and positions opened) in ``[since, until)``.

    *closed* (default: :func:`closed_positions` for the window) lets a caller pass rows
    it already filtered (the Tower drops broker smoke tests); *exclude_hashes* drops
    those open proposals from the opens count too. Every sleeve of *sleeves* gets a row
    (zeros included); ``other`` only when it has a trade.
    """
    start = since or _EPOCH
    rows = (
        list(closed)
        if closed is not None
        else closed_positions(conn, start=start, end=until, now=now)
    )
    skip = set(exclude_hashes)
    by_sid = sleeves_by_structure(conn, sleeves)
    dtes = entry_dtes(conn)
    opens: Counter[str] = Counter()
    for sid, phash, opened in conn.execute(
        "SELECT id, open_proposal_hash, opened_at FROM open_structures"
    ):
        at = _ts(opened)
        if at is None or not start <= at < until or phash in skip:
            continue
        opens[sleeve_name(sleeves, dtes.get(str(sid)))] += 1
    costs: dict[str, float] = {}
    slip: dict[str, float] = {}
    for r in execution_costs(conn, _EPOCH, _FOREVER).rows:
        if r.structure_id is None:
            continue
        costs[r.structure_id] = costs.get(r.structure_id, 0.0) + r.commission + r.fees
        if r.kind == "open":
            slip[r.structure_id] = slip.get(r.structure_id, 0.0) + r.realised_usd
    groups: dict[str, list[ClosedPosition]] = {}
    for c in rows:
        groups.setdefault(by_sid.get(c.structure_id, OTHER), []).append(c)
    m_start = measurement_start(conn)
    weeks = (until - m_start).total_seconds() / (7 * _DAY_S) if m_start is not None else None
    out = [
        _stats(conn, s.name, s, groups.get(s.name, []), opens[s.name], costs, slip, weeks)
        for s in sorted(sleeves, key=lambda s: s.dte_min)
    ]
    if groups.get(OTHER) or opens[OTHER]:
        out.append(
            _stats(conn, OTHER, None, groups.get(OTHER, []), opens[OTHER], costs, slip, weeks)
        )
    return SleeveReport(
        since=since,
        until=until,
        measurement_start=m_start,
        weeks=None if weeks is None else max(weeks, 0.0),
        sleeves=out,
    )


def _money(v: float | None) -> str:
    return "n/a" if v is None else f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def sleeve_report_lines(rep: SleeveReport) -> list[str]:
    """Plain-text report (``arc scorecard sleeves``; the Analyst's context reads it)."""
    head = "sleeves"
    head += f" since {rep.since.astimezone(ET):%Y-%m-%d}" if rep.since else " (all time)"
    start = f"{rep.measurement_start.astimezone(ET):%Y-%m-%d}" if rep.measurement_start else "n/a"
    weeks = "n/a" if rep.weeks is None else f"{rep.weeks:.1f}"
    out = [
        head,
        f"measurement start {start} · {weeks} of {rep.max_weeks} weeks",
        f"rule (D97): {rep.rule}",
    ]
    for s in rep.sleeves:
        window = f" {s.window} DTE" if s.window else " (entry DTE fits no sleeve)"
        ci = (
            f"{rep.ci_level:.0%} CI [{_money(s.ci_low)}, {_money(s.ci_high)}]"
            if s.ci_low is not None
            else "CI n/a"
        )
        win = "n/a" if s.win_rate is None else f"{s.win_rate:.0%}"
        pct = "n/a" if s.mean_pct_of_max_loss is None else f"{s.mean_pct_of_max_loss:+.0%}"
        days = "n/a" if s.mean_days_held is None else f"{s.mean_days_held:.1f}"
        verdict_txt = (
            ""
            if s.verdict is None
            else f" · verdict {s.verdict}{'' if s.ready else ' (not ready)'}"
        )
        out.append(
            f"  {s.sleeve}{window}: opens {s.opens} · closed n={s.closed} · win {win} · "
            f"mean {_money(s.mean_pnl)} · median {_money(s.median_pnl)} · "
            f"mean net {_money(s.mean_net_pnl)} · {ci}{verdict_txt}"
        )
        exits = ", ".join(f"{k} {v}" for k, v in s.exit_reasons.items()) or "none"
        out.append(
            f"    P&L/max loss {pct} · days held {days} · costs {_money(-s.costs)} · "
            f"entry slippage {_money(s.entry_slippage)} · MAE {_money(s.mean_mae)} · "
            f"MFE {_money(s.mean_mfe)} (n={s.n_excursion}) · exits {exits}"
        )
    return out


def _whole_money(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.0f}"


def day_sleeve_lines(
    conn: sqlite3.Connection, day: _dt.date, sleeves: Sequence[DteSleeve], *, now: _dt.datetime
) -> list[str]:
    """E22.5 (D97): the Day Recap's per-sleeve lines for ET *day*.

    ``Short 10–29: 2 opened · 1 closed +$84 · 3/5 open``: positions opened on *day*,
    positions fully closed on *day* with their realised P&L (the scorecard's
    :func:`closed_positions`), and the sleeve's open positions now vs its slot cap (by
    entry DTE). Empty for a single-window profile (one implicit ``default`` sleeve), so
    its recap is unchanged. An ``Other`` line appears only when a trade fits no sleeve.
    """
    named = [s for s in sleeves if s.name != DEFAULT_SLEEVE]
    if not named:
        return []
    start = _dt.datetime.combine(day, _dt.time.min, tzinfo=ET)
    end = start + _dt.timedelta(days=1)
    dtes = entry_dtes(conn)
    opened: Counter[str] = Counter()
    held: Counter[str] = Counter()
    for sid, opened_at, status in conn.execute("SELECT id, opened_at, status FROM open_structures"):
        name = sleeve_name(named, dtes.get(str(sid)))
        at = _ts(opened_at)
        if at is not None and start <= at < end:
            opened[name] += 1
        if status == "open":
            held[name] += 1
    closed: dict[str, list[float]] = {}
    for c in closed_positions(conn, start=start, end=end, now=now):
        closed.setdefault(sleeve_name(named, dtes.get(c.structure_id)), []).append(c.realised_pnl)
    out = []
    rows: list[tuple[str, str, int | None]] = [
        (s.name, f"{s.name.capitalize()} {s.dte_min}–{s.dte_max}", s.max_positions)
        for s in sorted(named, key=lambda s: s.dte_min)
    ]
    if opened[OTHER] or held[OTHER] or closed.get(OTHER):
        rows.append((OTHER, "Other", None))
    for name, label, cap in rows:
        pnl = closed.get(name, [])
        closed_txt = f"{len(pnl)} closed" + (f" {_whole_money(sum(pnl))}" if pnl else "")
        open_txt = f"{held[name]}/{cap} open" if cap is not None else f"{held[name]} open"
        out.append(f"{label}: {opened[name]} opened · {closed_txt} · {open_txt}")
    return out
