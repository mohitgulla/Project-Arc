"""E22.3 (D97): free position slots per entry-DTE sleeve, as Research and the loop see them.

Pure bookkeeping, no I/O: the caller passes the open book's entry DTEs (as the gate
groups them, :func:`arc.pipeline.market.open_entry_dtes`), today's opens per sleeve,
the account's equity and (cash profiles) settled cash.

* A sleeve's free slots = ``min(sleeve cap - held, global free)``; an uncapped sleeve
  (every single-window profile) has the global ``max_open_positions - held``.
* ``Free slots: short 3/5, swing 1/5 · settled cash $X · per-name cap $2,500`` is the
  code-built line in the Research prompt (single window: ``Free slots: n/N``).
* :meth:`FreeSlots.pressure` is the loop's condition for ``max_idle_with_free_slots``:
  a free slot in any sleeve **and** a sleeve with no open today.
* :meth:`FreeSlots.asked` is how many names Research is asked to rank:
  ``min(research_min_ranked, total free x 2, pool size)``.
"""

from __future__ import annotations

from decimal import Decimal  # noqa: TC003 - pydantic fields
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from arc.account_profiles import sleeve_for_dte

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.account_profiles import DteSleeve

__all__ = ["FreeSlots", "SleeveFree", "free_slots"]

_FORBID = ConfigDict(extra="forbid", frozen=True)


class SleeveFree(BaseModel):
    """One sleeve's free slots against its cap (the global cap when uncapped)."""

    model_config = _FORBID

    name: str
    free: int = Field(..., ge=0)
    cap: int = Field(..., ge=0)
    opens_today: int = Field(0, ge=0)


class FreeSlots(BaseModel):
    """Free slots of the book, per sleeve (see the module doc)."""

    model_config = _FORBID

    sleeves: list[SleeveFree]
    single: bool  # a single-window profile: renders ``Free slots: n/N``
    settled_cash: Decimal | None = None  # cash_settled profiles only
    options_buying_power: Decimal | None = None  # D129: margin profiles (None = unknown)
    per_name_cap: Decimal

    @property
    def total_free(self) -> int:
        if self.single:
            return self.sleeves[0].free if self.sleeves else 0
        return sum(s.free for s in self.sleeves)

    def pressure(self) -> bool:
        """A free slot in any sleeve and a sleeve with no open today (E22.3 loop rule)."""
        return any(s.free > 0 for s in self.sleeves) and any(
            s.opens_today == 0 for s in self.sleeves
        )

    def asked(self, min_ranked: int, pool: int) -> int:
        """Names Research is asked to rank: ``min(min_ranked, total free x 2, pool)``."""
        return max(min(min_ranked, self.total_free * 2, pool), 0)

    def line(self) -> str:
        if self.single:
            s = self.sleeves[0] if self.sleeves else None
            text = "Free slots: 0/0" if s is None else f"Free slots: {s.free}/{s.cap}"
        else:
            text = "Free slots: " + ", ".join(f"{s.name} {s.free}/{s.cap}" for s in self.sleeves)
        if self.settled_cash is not None:
            text += f" \u00b7 settled cash ${self.settled_cash:,.0f}"
        if self.options_buying_power is not None:  # D129
            text += f" \u00b7 buying power ${self.options_buying_power:,.0f}"
        return text + f" \u00b7 per-name cap ${self.per_name_cap:,.0f}"


def free_slots(
    sleeves: Sequence[DteSleeve],
    entry_dtes: Sequence[int | None],
    *,
    max_open_positions: int,
    single: bool,
    per_name_cap: Decimal,
    settled_cash: Decimal | None = None,
    options_buying_power: Decimal | None = None,
    opens_today: Mapping[str, int] | None = None,
) -> FreeSlots:
    """Count each sleeve's free slots like the gate (unknown entry DTE fills every sleeve).

    ``settled_cash`` renders on cash profiles, ``options_buying_power`` (D129) on margin
    profiles; pass only the one the profile uses.
    """
    global_free = max(max_open_positions - len(entry_dtes), 0)
    today = opens_today or {}
    out: list[SleeveFree] = []
    for s in sleeves:
        held = sum(1 for d in entry_dtes if d is None or sleeve_for_dte(sleeves, d) == s)
        if s.max_positions is None:
            cap, free = max_open_positions, global_free
        else:
            cap, free = s.max_positions, min(max(s.max_positions - held, 0), global_free)
        out.append(SleeveFree(name=s.name, free=free, cap=cap, opens_today=today.get(s.name, 0)))
    return FreeSlots(
        sleeves=out,
        single=single,
        settled_cash=settled_cash,
        options_buying_power=options_buying_power,
        per_name_cap=per_name_cap,
    )
