"""E14.6 (D60): the one rendering of a ``retail_sentiment`` reading (pure).

Shared by the persona prompts (:mod:`arc.personas.retail_sentiment`) and the read-only
Tower, which may not import :mod:`arc.personas`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["sentiment_fact", "window_text"]


def window_text(minutes: float | None) -> str | None:
    """``45m`` / ``2.7h`` / ``21.6d`` (``None`` when unknown)."""
    if minutes is None:
        return None
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def sentiment_fact(p: Mapping[str, Any]) -> str:
    """``ST 80% bull (10 tagged, 2.7h)`` or ``ST too few tags (3 tagged, 9.1h)``.

    The ratio is the stored code-computed ``bull_ratio``; ``None`` (fewer than
    ``min_tagged`` tags) reads "too few tags", never 0% or 100%.
    """
    tagged = int(p.get("tagged") or 0)
    win = window_text(p.get("window_minutes"))
    detail = f"{tagged} tagged" + (f", {win}" if win else "")
    ratio = p.get("bull_ratio")
    if ratio is None:
        return f"ST too few tags ({detail})"
    return f"ST {float(ratio) * 100:.0f}% bull ({detail})"
