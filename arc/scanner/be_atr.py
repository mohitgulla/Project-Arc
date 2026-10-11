"""E16.5 (D76): breakeven in ATR terms per structure + the optional realism filter.

``be_atr = |BE − spot| / (ATR14 · √DTE)``: how far a structure's breakeven sits from
spot, measured in the stock's own average daily range scaled to the days left. For a
debit structure the relevant breakeven is the one in the trade's direction (the
highest for a call debit, the lowest for a put debit). Credit structures have no
single direction: their value is ``None`` and the filter never touches them.

The filter (``scanner.max_be_atr`` in ``config/ranking.yaml``, ``None`` = off, the
default) drops debit structures whose ``be_atr`` is above the limit **before** the
menu is ranked and cut. A ticker without ATR14 (no E16.2 technicals on its
``regime`` entry) keeps every structure: an optional filter, not a safety rule.

Pure: no I/O, no clock, no LLM. Never a gate input.
"""

from __future__ import annotations

import dataclasses
import datetime as dt  # noqa: TC003 - Protocol attribute type
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from arc.journal.analytics import be_atr_multiple, debit_direction, directional_breakeven
from arc.structures import parse_occ

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from enum import StrEnum

    from arc.models import Structure

__all__ = [
    "BE_ATR_MAX",
    "BE_ATR_MIN",
    "BeFilterResult",
    "ScannerFilters",
    "filter_menu",
    "load_scanner_filters",
    "structure_be_atr",
]

DEFAULT_RANKING_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "ranking.yaml"
#: Registry / config bounds of ``scanner.max_be_atr`` (card E16.5).
BE_ATR_MIN = 0.5
BE_ATR_MAX = 5.0


class _Candidate(Protocol):
    """What the filter reads off a scanner candidate (:class:`arc.scanner.ScanCandidate`).

    A Protocol, not the class: importing :mod:`arc.scanner.scan` would pull market
    data into :mod:`arc.control` (and so the read-only tower) via this module.
    """

    @property
    def structure(self) -> Structure: ...
    @property
    def strategy(self) -> StrEnum: ...
    @property
    def expiration(self) -> dt.date: ...


class ScannerFilters(BaseModel):
    """``scanner:`` in ``config/ranking.yaml`` (E16.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_be_atr: float | None = Field(
        None,
        ge=BE_ATR_MIN,
        le=BE_ATR_MAX,
        description="Drop debit structures whose directional breakeven is more than this "
        "many ATR14·√DTE from spot, before the menu is ranked; None = off",
    )


def load_scanner_filters(
    path: Path | str | None = None, *, overrides: Mapping[tuple[str, ...], object] | None = None
) -> ScannerFilters:
    """``scanner:`` of ``config/ranking.yaml`` (or *path*) with the D26 *overrides*
    (``path -> value`` from the file root, e.g. ``("scanner", "max_be_atr")``)."""
    from arc.utils.yamlpatch import apply_overrides

    p = Path(path) if path is not None else DEFAULT_RANKING_PATH
    data = apply_overrides(yaml.safe_load(p.read_text()) or {}, overrides)
    return ScannerFilters.model_validate(data.get("scanner") or {})


def structure_be_atr(st: Structure, spot: float, atr14: float | None) -> float | None:
    """``be_atr`` of the breakeven in *st*'s direction; ``None`` for credit structures,
    a debit with calls and puts both bought, or no ATR14."""
    longs = [
        "call" if parse_occ(leg.occ_symbol).kind.name == "CALL" else "put"
        for leg in st.legs
        if leg.side.value == "long"
    ]
    direction = debit_direction(float(st.net_debit_credit), longs)
    be = directional_breakeven([float(b) for b in st.breakevens], direction)
    if be is None:
        return None
    return be_atr_multiple(be, spot, atr14, st.dte)


@dataclasses.dataclass
class BeFilterResult[C: _Candidate]:
    """What :func:`filter_menu` kept and dropped for one ticker."""

    kept: list[C]
    dropped: list[tuple[C, float]] = dataclasses.field(default_factory=list)
    # id(candidate) -> be_atr (None for credit structures), every candidate seen
    values: dict[int, float | None] = dataclasses.field(default_factory=dict)

    def dropped_rows(self) -> list[dict[str, Any]]:
        """Journal payload: one row per dropped structure."""
        return [
            {
                "strategy": c.strategy.value,
                "expiration": c.expiration.isoformat(),
                "legs": [leg.occ_symbol for leg in c.structure.legs],
                "breakevens": [float(b) for b in c.structure.breakevens],
                "be_atr": round(v, 4),
            }
            for c, v in self.dropped
        ]


def filter_menu[C: _Candidate](
    cands: Sequence[C],
    *,
    spot: float,
    atr14: float | None,
    max_be_atr: float | None,
) -> BeFilterResult[C]:
    """Drop debit candidates with ``be_atr > max_be_atr`` (order of the rest kept).

    ``max_be_atr`` ``None`` (off) or no ATR14 keeps every candidate.
    """
    res: BeFilterResult[C] = BeFilterResult(kept=[])
    for c in cands:
        v = structure_be_atr(c.structure, spot, atr14)
        res.values[id(c)] = v
        if max_be_atr is not None and v is not None and v > max_be_atr:
            res.dropped.append((c, v))
            continue
        res.kept.append(c)
    return res
