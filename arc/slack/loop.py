"""D36: the one-line status root each trading-loop slot posts in #arc-investor.

Pure rendering. One of three states, then the same facts every time:

    :white_check_mark: 2026-09-28 09:40ET • Portfolio: $101,234 • P&L: +$312
        • Orders: 3/200 • BUY: SPY
    :hourglass_flowing_sand: … • PENDING: SPY
    :heavy_multiplication_x: … • HOLD

D65: the line is followed by a bold-italic *headline* of at most two lines
(``_*…*_``) that says what the loop did and why, in plain words: the opens
funnel first, then the exits. It is built from what the chain journaled
(:func:`arc.routines.loop.loop_headline`), so it is re-rendered with the root.
Everything else (persona cards, the proposal card, the ``[Routines]`` metadata)
stays in the thread.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic fields
import enum

from pydantic import BaseModel, ConfigDict, Field

from arc.utils.calendar import ET


class LoopOutcome(enum.StrEnum):
    TRADED = "traded"  # a trade filled (buy or sell) in this loop
    PENDING = "pending"  # a proposal awaits manual approval, or a ladder is working
    HOLD = "hold"  # no action


_EMOJI = {
    LoopOutcome.TRADED: ":white_check_mark:",
    LoopOutcome.PENDING: ":hourglass_flowing_sand:",
    LoopOutcome.HOLD: ":heavy_multiplication_x:",
}


class LoopRoot(BaseModel):
    """Everything the root line shows (stored per chain, re-rendered on updates)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: _dt.datetime
    equity: float | None = None
    day_pnl: float | None = None
    orders_used: int | None = None
    orders_limit: int | None = None
    buys: list[str] = Field(default_factory=list)  # filled opens, ranked
    sells: list[str] = Field(default_factory=list)  # filled closes, ranked
    pending: list[str] = Field(default_factory=list)  # awaiting manual approval
    working: list[str] = Field(default_factory=list)  # approved, ladder running
    no_change: bool = False
    timeout: bool = False
    skipped: str | None = None  # the slot never ran (why)
    headline: list[str] = Field(default_factory=list)  # D65: ≤2 plain-text lines

    @property
    def outcome(self) -> LoopOutcome:
        if self.buys or self.sells:
            return LoopOutcome.TRADED
        if self.pending or self.working:
            return LoopOutcome.PENDING
        return LoopOutcome.HOLD

    def text(self) -> str:
        return loop_status_line(self)


def slot_stamp(slot: _dt.datetime) -> str:
    """``YYYY-MM-DD HH:MMET`` (owner's format, no space before ET), DST-correct."""
    return f"{slot.astimezone(ET):%Y-%m-%d %H:%M}ET"


def _money(v: float | None, *, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    if signed:
        sign = "+" if v >= 0 else "-"
        return f"{sign}${abs(v):,.0f}"
    return f"${v:,.0f}"


def loop_status_line(root: LoopRoot) -> str:
    """The root line for *root* (see the module docstring)."""
    facts = [
        slot_stamp(root.slot),
        f"Portfolio: {_money(root.equity)}",
        f"P&L: {_money(root.day_pnl, signed=True)}",
        "Orders: "
        + (
            f"{root.orders_used}/{root.orders_limit}"
            if root.orders_used is not None and root.orders_limit is not None
            else "n/a"
        ),
    ]
    action: list[str] = []
    if root.buys:
        action.append("BUY: " + ", ".join(root.buys))
    if root.sells:
        action.append("SELL: " + ", ".join(root.sells))
    if not action:
        if root.pending:
            action.append("PENDING: " + ", ".join(root.pending))
        if root.working:
            action.append("WORKING: " + ", ".join(root.working))
    if not action:
        hold = "HOLD"
        if root.skipped:
            hold = f"HOLD (skipped: {root.skipped})"
        elif root.timeout:
            hold = "HOLD (timeout)"
        elif root.no_change:
            hold = "HOLD (skip)"
        action.append(hold)
    line = f"{_EMOJI[root.outcome]} " + " • ".join([*facts, *action])
    head = [h for h in (root.headline or _flag_headline(root)) if h.strip()][:HEADLINE_LINES]
    return "\n".join([line, *(_bold_italic(h) for h in head)])


HEADLINE_LINES = 2
HEADLINE_MAX = 160  # chars per headline line (a Slack line on a laptop)


def _flag_headline(root: LoopRoot) -> list[str]:
    """Headline for a loop that never reached the personas (skip / timeout / no change)."""
    if root.skipped:
        return [f"Slot skipped ({root.skipped}); nothing was evaluated."]
    if root.timeout:
        return ["Loop hit its deadline; the steps after it were skipped this slot."]
    if root.no_change:
        return [
            "Inputs unchanged since the last full loop; personas skipped, open orders carry on."
        ]
    return []


def _bold_italic(text: str) -> str:
    """``_*text*_``: Slack bold + italic, with the marker characters made safe."""
    clean = " ".join(text.split())
    clean = clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    clean = clean.replace("*", "∗").replace("_", " ").replace("~", "-").replace("`", "'")
    if len(clean) > HEADLINE_MAX:
        clean = clean[: HEADLINE_MAX - 1].rstrip(" ,;·") + "…"
    return f"_*{clean}*_"


__all__ = ["HEADLINE_LINES", "LoopOutcome", "LoopRoot", "loop_status_line", "slot_stamp"]
