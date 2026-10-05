"""Ticker extraction for ingest (D28, tightened by E12.3 / D51).

Deterministic, no LLM. Each pattern's hits are validated against the accepted
symbol set (seed list ∪ symbol master in ``seed`` mode, the seed list alone in
``strict`` mode):

* cashtags ``$NVDA`` / ``$brk.b`` (any case, any length, stop words ignored);
* labelled symbols ``(NVDA)``, ``(NYSE: NVDA)``, ``(Symbol: XLE)`` and
  ``ticker symbol TE`` (any length). Words in ``paren_stop_words`` (``(AI)`` for
  artificial intelligence, ``(COLA)`` for cost-of-living adjustment) never match here;
* bare upper-case words ``NVDA``, matched case-sensitively so ordinary words never
  match. A bare word must not be a stop word, must have at least
  ``min_symbol_len`` letters, and (E12.3) a bare word shorter than
  ``bare_min_len`` (default 4: ``ET``, ``SA``, ``TD``, ``RBC``) counts only when it
  is in *bare_allow* (the core + momentum tiers).

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
    r"\(\s*(?:NYSE|NASDAQ|Nasdaq|NYSEARCA|NYSE Arca|AMEX|CBOE|Symbol|symbol)?\s*:?\s*"
    r"([A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z])?)\s*\)"
)
# "ticker symbol DKNG", "ticker symbol TE": a labelled symbol in transcripts.
_LABELLED = re.compile(r"\b[Tt]icker symbol,?\s+([A-Z][A-Z0-9]{0,5}(?:\.[A-Z])?)(?![A-Za-z0-9])")
_BARE = re.compile(r"(?<![A-Za-z0-9$.\-])([A-Z][A-Z0-9]{0,5}(?:\.[A-Z])?)(?![A-Za-z0-9])")


def extract_tickers(
    text: str,
    accepted: Collection[str],
    cfg: ExtractionConfig,
    *,
    bare_allow: Collection[str] = (),
) -> list[str]:
    """Symbols in *text* that are in *accepted* (normalised, first-appearance order).

    *bare_allow* (normalised symbols, E12.3: core + momentum) are the only bare words
    shorter than ``cfg.bare_min_len`` that count.
    """
    plain = html.unescape(text)
    stop = {w.upper() for w in cfg.stop_words}
    paren_stop = {w.upper() for w in cfg.paren_stop_words}
    allow = {normalize_symbol(s) for s in bare_allow}
    hits: list[tuple[int, str]] = []
    for m in _CASHTAG.finditer(plain):
        hits.append((m.start(), normalize_symbol(m.group(1))))
    for pat in (_PAREN, _LABELLED):
        for m in pat.finditer(plain):
            if m.group(1) in paren_stop:
                continue
            hits.append((m.start(1), normalize_symbol(m.group(1))))
    for m in _BARE.finditer(plain):
        sym = m.group(1)
        n = len(sym.replace(".", ""))
        if n < cfg.min_symbol_len or sym in stop:
            continue
        norm = normalize_symbol(sym)
        if n < cfg.bare_min_len and norm not in allow:
            continue
        hits.append((m.start(), norm))
    out: list[str] = []
    seen: set[str] = set()
    for _, sym in sorted(hits):
        if sym in accepted and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out
