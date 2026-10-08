"""E14.6 (D60): Stocktwits sentiment as persona context (pure, code-built).

Behind ``personas.retail_sentiment_context`` (default off): the Scout gets a "Retail
sentiment" block, Research gets one ``ST …`` fact per pool line. Every number comes
from a stored ``retail_sentiment`` entry; untagged messages never enter the ratio and
fewer than ``min_tagged`` tags reads "too few tags", never 0% or 100%.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from arc.context.retail_sentiment import sentiment_fact, window_text

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Iterable, Mapping

    from arc.context.store import ContextSnapshot
    from arc.context.ttl import Ttl

__all__ = [
    "fresh_sentiment",
    "scout_block",
    "research_sentiment_facts",
    "scout_sentiment_lines",
    "sentiment_digest",
    "sentiment_fact",
    "window_text",
]

KIND = "retail_sentiment"


def fresh_sentiment(
    snapshot: ContextSnapshot, *, as_of: _dt.datetime, max_age: Ttl | None
) -> dict[str, dict[str, Any]]:
    """``ticker -> payload`` of the active ``retail_sentiment`` entries still inside
    *max_age* (the ``retail_buzz`` category window) at *as_of*."""
    out: dict[str, dict[str, Any]] = {}
    for e in snapshot.of_kind(KIND):
        if max_age is not None and not (e.valid_from <= as_of < max_age.expires_at(e.valid_from)):
            continue
        prev = out.get(e.subject)
        if prev is None or str(e.payload.get("as_of")) > str(prev.get("as_of")):
            out[e.subject] = dict(e.payload)
    return out


def scout_sentiment_lines(readings: Mapping[str, Mapping[str, Any]], *, top: int) -> list[str]:
    """The Scout's block: top *top* tickers by tagged count (then ticker).

    Recorded as a prompt input, so ``[]`` (flag on, nothing fresh) renders "no info".
    """
    ranked = sorted(readings.items(), key=lambda kv: (-int(kv[1].get("tagged") or 0), kv[0]))
    lines = []
    for ticker, p in ranked[: max(0, top)]:
        fact = sentiment_fact(p).removeprefix("ST ")
        lines.append(f"- {ticker} · {fact} of {int(p.get('messages') or 0)} messages")
    return lines


def scout_block(lines: list[str] | None, *, min_tagged: int | None = None) -> list[str]:
    """The rendered section (``[]`` when the flag is off: the prompt is unchanged)."""
    if lines is None:
        return []
    head = "## Retail sentiment (Stocktwits, latest messages per ticker; context only)"
    if not lines:
        return ["", head, "- no info"]
    floor = f" fewer than {min_tagged} tags = too few tags;" if min_tagged else ""
    return [
        "",
        head,
        "Bull % = bullish / user-tagged messages, counted by code; untagged messages are"
        f" ignored;{floor} the time span is how long the messages cover (shorter = busier).",
        "Crowd mood is a weak, often contrarian signal: never a thesis on its own.",
        *lines,
    ]


def research_sentiment_facts(
    readings: Mapping[str, Mapping[str, Any]], tickers: Iterable[str]
) -> dict[str, str]:
    """``ticker -> "ST 80% bull (10 tagged, 2.7h)"`` for the pool tickers that have one."""
    return {t: sentiment_fact(readings[t]) for t in tickers if t in readings}


def sentiment_digest(
    readings: Mapping[str, Mapping[str, Any]], tickers: Iterable[str]
) -> list[str]:
    """D31 loop digest entries ``retail_sentiment:<ticker>@<as_of>`` (as_of only)."""
    return sorted(f"{KIND}:{t}@{readings[t].get('as_of')}" for t in tickers if t in readings)
