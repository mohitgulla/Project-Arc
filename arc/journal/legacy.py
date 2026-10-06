"""D54 / D56: read history written under a persona's earlier names.

The journal, ``context_entries``, ``routine_runs`` and ``persona_calls`` are
append-only, so rows keep the name their writer had at the time. Two renames happened:

* **D54 (E5.12, migration 022):** the 30-min doc reader ``scout`` became ``sweep``;
  ``scout`` was then reused for the slow-feed Scout persona (E5.13).
* **D56 (E13.1, migration 024):** ``sweep`` became ``scalp`` and ``director``
  became ``research``.
* **D56 (E13.2, migration 025):** the Investor and Auditor personas were removed.
  Jobs: ``investor`` -> ``broker``, ``execute`` -> ``broker.execute``,
  ``investor.exits`` -> ``quant.exits``, ``auditor`` -> ``broker.reconcile``.
  Decisions: ``investor`` rows are the **Broker** except ``stage='exit'`` rows
  (the position-review chain), which are **Quant**; ``auditor`` rows are the
  Broker's reconcile (label ``Broker (reconcile)``). These hops match exact names
  only (``investor.exits`` is its own hop, never ``broker.exits``).
* **D56 (E13.9, no migration):** chain steps ``quant`` -> ``quant.open``, ``risk`` ->
  ``risk.open``, ``propose`` -> ``quant.propose``. These hops are ``job_only``: a
  decision's ``persona='quant'`` is still the Quant, never ``quant.open``.

Readers map stored values through :data:`RENAME_CHAIN`, hop by hop. A hop applies
when the value equals ``hop.old`` (or starts with ``hop.old + "."``, e.g.
``sweep.overnight``) and the row predates that hop's cutover, or no cutover is
recorded (a store not yet migrated holds only pre-rename rows). So a ``scout`` row
from before 022 reads as **Scalp** (scout -> sweep -> scalp), a ``scout`` row after
022 stays the slow-feed **Scout**, and ``director`` reads as **Research** (the name
is never reused).

Reason codes compose across hops the same way (``scout_candidate`` ->
``sweep_candidate`` -> ``scalp_candidate``). No code is ever reused, so they ignore
the cutovers.

Each cutover instant is written once by its migration into ``routine_state``.
Pure functions plus reads; no writes.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from arc.context.ttl import from_db, to_db

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Mapping

__all__ = [
    "BROKER_CUTOVER_KEY",
    "CUTOVER_KEY",
    "LEGACY_REASON_CODES",
    "RENAME_CHAIN",
    "SCALP_CUTOVER_KEY",
    "RenameHop",
    "cutover",
    "cutovers",
    "job_clause",
    "job_name",
    "legacy_names",
    "persona_key",
    "persona_label",
    "reason_code",
]


class RenameHop(BaseModel):
    """One persona rename: ``old`` rows written before the cutover instant are ``new``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    old: str = Field(description="Stored name before the rename (e.g. 'sweep')")
    new: str = Field(description="Name after the rename (e.g. 'scalp')")
    cutover_key: str = Field(description="routine_state key holding the rename instant")
    reason_codes: dict[str, str] = Field(
        default_factory=dict, description="Stored reason codes this rename renamed"
    )
    exact: bool = Field(
        default=False, description="Match the exact name only (no ``old.<suffix>`` names)"
    )
    persona: str | None = Field(
        default=None,
        description="Persona key when read as a decision's persona (default: ``new``)",
    )
    stages: dict[str, str] = Field(
        default_factory=dict,
        description="Persona key by decision stage, overriding ``persona`` (E13.2: exit -> quant)",
    )
    job_only: bool = Field(
        default=False,
        description="A step rename only: never applied to a stored persona value "
        "(E13.9: ``quant`` the step became ``quant.open``; ``quant`` the persona stays)",
    )


#: D54 cutover (migration 022): ``scout`` -> ``sweep``.
CUTOVER_KEY = "rename:scout_to_sweep"
#: D56 cutover (migration 024): ``sweep`` -> ``scalp`` and ``director`` -> ``research``.
SCALP_CUTOVER_KEY = "rename:sweep_to_scalp"
#: D56 cutover (migration 025): Investor/Auditor removed (Broker, Quant exits, Ops).
BROKER_CUTOVER_KEY = "rename:investor_to_broker"
#: E13.9 (D56): open-path step renames. No migration records this key, so the hops
#: apply to every stored row: the old step names are never reused as job names.
OPEN_PATH_CUTOVER_KEY = "rename:open_path_steps"

RENAME_CHAIN: tuple[RenameHop, ...] = (
    RenameHop(
        old="scout",
        new="sweep",
        cutover_key=CUTOVER_KEY,
        reason_codes={"scout_candidate": "sweep_candidate"},
    ),
    RenameHop(
        old="sweep",
        new="scalp",
        cutover_key=SCALP_CUTOVER_KEY,
        reason_codes={"sweep_candidate": "scalp_candidate"},
    ),
    RenameHop(
        old="director",
        new="research",
        cutover_key=SCALP_CUTOVER_KEY,
        reason_codes={
            "director_excluded": "research_excluded",
            "director_no_trade": "research_no_trade",
        },
    ),
    # E13.2: the exact-name hops come first so ``investor.exits`` never reads as
    # ``broker.exits``.
    RenameHop(
        old="investor.exits",
        new="quant.exits",
        cutover_key=BROKER_CUTOVER_KEY,
        exact=True,
        persona="quant",
    ),
    RenameHop(
        old="execute",
        new="broker.execute",
        cutover_key=BROKER_CUTOVER_KEY,
        exact=True,
        persona="broker",
    ),
    RenameHop(
        old="investor",
        new="broker",
        cutover_key=BROKER_CUTOVER_KEY,
        exact=True,
        stages={"exit": "quant"},
    ),
    RenameHop(
        old="auditor",
        new="broker.reconcile",
        cutover_key=BROKER_CUTOVER_KEY,
        exact=True,
        persona="broker",
    ),
    # E13.9: chain steps quant -> quant.open, risk -> risk.open, propose -> quant.propose
    # (routine_runs.job / context produced_by only; decision personas are unchanged).
    *(
        RenameHop(old=old, new=new, cutover_key=OPEN_PATH_CUTOVER_KEY, exact=True, job_only=True)
        for old, new in (
            ("quant", "quant.open"),
            ("risk", "risk.open"),
            ("propose", "quant.propose"),
        )
    ),
)


def _compose_reason_codes() -> dict[str, str]:
    """Every stored code a hop renamed -> the code it reads as today (hops composed)."""
    step = {old: new for hop in RENAME_CHAIN for old, new in hop.reason_codes.items()}
    out: dict[str, str] = {}
    for old, new in step.items():
        while new in step:
            new = step[new]
        out[old] = new
    return out


#: Stored reason codes renamed by D54/D56 -> the current code.
LEGACY_REASON_CODES: dict[str, str] = _compose_reason_codes()


def cutover(conn: sqlite3.Connection, key: str = CUTOVER_KEY) -> _dt.datetime | None:
    """One rename instant (ET-aware), or ``None`` on a store without it."""
    try:
        row = conn.execute("SELECT value FROM routine_state WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:  # no routine_state table (minimal fixture stores)
        return None
    if row is None or not row[0]:
        return None
    return from_db(str(row[0]))


def cutovers(conn: sqlite3.Connection) -> dict[str, _dt.datetime]:
    """Every recorded rename instant of :data:`RENAME_CHAIN`, by ``routine_state`` key."""
    out: dict[str, _dt.datetime] = {}
    for key in dict.fromkeys(h.cutover_key for h in RENAME_CHAIN):
        at = cutover(conn, key)
        if at is not None:
            out[key] = at
    return out


def _applies(hop: RenameHop, at: _dt.datetime | None, cuts: Mapping[str, _dt.datetime]) -> bool:
    # No cutover recorded (pre-migration copy) or no timestamp: the row predates it.
    cut = cuts.get(hop.cutover_key)
    return cut is None or at is None or at < cut


def _matches(hop: RenameHop, name: str) -> bool:
    return name == hop.old or (not hop.exact and name.startswith(f"{hop.old}."))


def _new_matches(hop: RenameHop, name: str) -> bool:
    return name == hop.new or (not hop.exact and name.startswith(f"{hop.new}."))


def job_name(
    job: str,
    at: _dt.datetime | None,
    cuts: Mapping[str, _dt.datetime],
    *,
    steps: bool = True,
) -> str:
    """A stored job / persona name -> its current one (``sweep.overnight`` -> ``scalp.overnight``).

    *cuts* is :func:`cutovers` of the store the row came from. ``steps=False`` skips
    the E13.9 step-only hops (for a value that names a persona, not a job).
    """
    for hop in RENAME_CHAIN:
        if hop.job_only and not steps:
            continue
        if _matches(hop, job) and _applies(hop, at, cuts):
            job = hop.new + job[len(hop.old) :]
    return job


def persona_key(
    value: str,
    at: _dt.datetime | None,
    cuts: Mapping[str, _dt.datetime],
    *,
    stage: str | None = None,
) -> str:
    """The current persona key for a stored ``persona`` / ``produced_by`` value.

    E13.2: a hop with a persona mapping (``investor`` -> ``broker``, or ``quant`` for
    a ``stage='exit'`` row; ``auditor`` -> ``broker``) ends the chain there, so the
    result is always a :class:`~arc.journal.reasons.JournalPersona` value.
    """
    for hop in RENAME_CHAIN:
        if hop.job_only or not (_matches(hop, value) and _applies(hop, at, cuts)):
            continue
        if hop.stages or hop.persona is not None:
            if stage is not None and stage in hop.stages:
                return hop.stages[stage]
            return hop.persona or hop.new
        value = hop.new + value[len(hop.old) :]
    return value


def persona_label(
    value: str,
    at: _dt.datetime | None,
    cuts: Mapping[str, _dt.datetime] | None = None,
    *,
    stage: str | None = None,
) -> str:
    """Display label for a stored persona: ``Scalp``, ``Research``, ``Scalp (digest)``;
    E13.2: ``investor`` -> ``Broker`` (``Quant`` for exit rows), ``auditor`` ->
    ``Broker (reconcile)``."""
    cuts = cuts or {}
    key = persona_key(value, at, cuts, stage=stage)
    job = job_name(value, at, cuts, steps=False)
    # The job name carries the sub-job (``broker.reconcile``) when it is the same persona.
    name = job if job.partition(".")[0] == key.partition(".")[0] else key
    head, _, tail = name.partition(".")
    label = head[:1].upper() + head[1:]
    return f"{label} ({tail})" if tail else label


def legacy_names(job: str) -> list[tuple[str, str]]:
    """Stored names that read as the current *job*, each with the cutover bounding it.

    ``scalp.overnight`` -> ``[("sweep.overnight", SCALP_CUTOVER_KEY),
    ("scout.overnight", CUTOVER_KEY)]``. A reader that filters on the current name
    also matches those rows when they predate that cutover (cf. :func:`job_name`).
    """
    out: list[tuple[str, str]] = []
    frontier = [job]
    for hop in reversed(RENAME_CHAIN):
        for name in list(frontier):
            if _new_matches(hop, name):
                old = hop.old + name[len(hop.new) :]
                out.append((old, hop.cutover_key))
                frontier.append(old)
    return out


def job_clause(
    conn: sqlite3.Connection, job: str, *, column: str = "job", at_column: str = "scheduled_for"
) -> tuple[str, list[str]]:
    """SQL ``(column = ? OR (column = ? AND at_column < ?) ...)`` matching *job* and its
    stored pre-rename names (each before its cutover, cf. :func:`legacy_names`)."""
    cuts = cutovers(conn)
    sql = f"{column} = ?"
    args: list[str] = [job]
    for old, key in legacy_names(job):
        sql += f" OR ({column} = ?"
        args.append(old)
        if key in cuts:
            sql += f" AND {at_column} < ?"
            args.append(to_db(cuts[key]))
        sql += ")"
    return f"({sql})", args


def reason_code(code: str) -> str:
    """Current reason code for a stored one (``scout_candidate`` -> ``scalp_candidate``)."""
    return LEGACY_REASON_CODES.get(code, code)
