"""Ticker extraction for ingest (D28): cashtags and exact symbols, validated against the master.

Deterministic, no LLM. Three patterns, each validated against the accepted
symbol set (seed list ∪ symbol master in ``seed`` mode, the seed list alone in
``strict`` mode):

* cashtags ``$NVDA`` / ``$brk.b`` (any case, any length);
* parenthesised symbols ``(NVDA)``, as in "Palantir (PLTR) wins ...";
* bare upper-case words ``NVDA`` of at least ``min_symbol_len`` letters that are
  not stop words ("CEO", "FDA", "US", ...), matched case-sensitively so ordinary
  words never match.

Results are in order of first appearance, de-duplicated.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from arc.universe.master import normalize_symbol

if TYPE_CHECKING:
    from collections.abc import Collection

    from arc.universe.config import ExtractionConfig

__all__ = ["extract_tickers"]

_CASHTAG = re.compile(
    r"(?<![A-Za-z0-9$])\$([A-Za-z][A-Za-z0-9]{0,5}(?:[.\-][A-Za-z])?)(?![A-Za-z0-9])"
)
_PAREN = re.compile(
    r"\(\s*(?:NYSE|NASDAQ|Nasdaq|NYSEARCA|AMEX|CBOE)?\s*:?\s*"
    r"([A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z])?)\s*\)"
)
_BARE = re.compile(r"(?<![A-Za-z0-9$.\-])([A-Z][A-Z0-9]{0,5}(?:\.[A-Z])?)(?![A-Za-z0-9])")


def extract_tickers(
    text: str,
    accepted: Collection[str],
    cfg: ExtractionConfig,
) -> list[str]:
    """Symbols in *text* that are in *accepted* (normalised, first-appearance order)."""
    plain = html.unescape(text)
    stop = {w.upper() for w in cfg.stop_words}
    hits: list[tuple[int, str]] = []
    for m in _CASHTAG.finditer(plain):
        hits.append((m.start(), normalize_symbol(m.group(1))))
    for m in _PAREN.finditer(plain):
        hits.append((m.start(1), normalize_symbol(m.group(1))))
    for m in _BARE.finditer(plain):
        sym = m.group(1)
        if len(sym.replace(".", "")) < cfg.min_symbol_len or sym in stop:
            continue
        hits.append((m.start(), normalize_symbol(sym)))
    out: list[str] = []
    seen: set[str] = set()
    for _, sym in sorted(hits):
        if sym in accepted and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out
