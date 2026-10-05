"""The entry DTE window and strike delta bands the personas are told (E3.4a, Analyst A-4).

One helper renders them from config, so the prompts can never drift from what the
scanner builds: the window is :attr:`arc.config.ArcSettings.entry_dte_window` (the
account profile's ``dte_min/dte_max``, else ``ARC_DTE_MIN/ARC_DTE_MAX``) and the
delta bands are the scanner bands of the strategies the profile maps a stance to
(credit short strikes, debit-vertical short legs, long legs).

No DTE range or delta band is written as a literal anywhere in :mod:`arc.personas`;
``tests/test_e34a_entry_window.py`` greps for it. The terms are recorded in the
persona call's ``prompt_inputs`` (``entry_terms``) so ``arc journal replay``
rebuilds the identical prompt.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from arc.config import ArcSettings

__all__ = [
    "DeltaBand",
    "EntryTerms",
    "entry_terms",
    "mentions_dte",
    "scrub_carried_text",
]

_FORBID = ConfigDict(extra="forbid")

# Scanner strategy names (arc.scanner.scan.ScanStrategy values) per band family.
# Kept as names so this leaf module needs no scanner import.
_CREDIT = frozenset({"bull_put", "bear_call", "iron_condor"})
_DEBIT_VERTICAL = frozenset({"bull_call_debit", "bear_put_debit"})
_LONG_SINGLE = frozenset({"long_call", "long_put"})

BandKind = Literal["credit_short", "debit_short", "long"]
_BAND_LABEL: dict[str, str] = {
    "credit_short": "short strikes",
    "debit_short": "debit-vertical short legs",
    "long": "long legs",
}


class DeltaBand(BaseModel):
    """One |delta| band the scanner applies (fractions, e.g. 0.16-0.30)."""

    model_config = _FORBID

    kind: BandKind
    lo: float = Field(..., gt=0.0, lt=1.0)
    hi: float = Field(..., gt=0.0, lt=1.0)

    def render(self) -> str:
        return f"{_BAND_LABEL[self.kind]} {self.lo * 100:.0f}-{self.hi * 100:.0f} delta"


class EntryTerms(BaseModel):
    """What every entry persona is told about expiry and strikes (from config)."""

    model_config = _FORBID

    account_profile: str
    dte_min: int = Field(..., ge=0)
    dte_max: int = Field(..., ge=0)
    bands: list[DeltaBand] = Field(default_factory=list)

    @property
    def window(self) -> str:
        return f"{self.dte_min}-{self.dte_max} DTE"

    def bands_text(self) -> str:
        return "; ".join(b.render() for b in self.bands) or "no strike band (no strategy)"

    def quant_lines(self) -> str:
        """The Quant's structure terms (replaces the old hard-coded D4 line)."""
        return (
            f"- Every menu structure is inside the {self.window} entry window set by the "
            f"account profile `{self.account_profile}`; do not reject or skip a menu item "
            "on DTE.\n"
            f"- Strike bands the scanner applied: {self.bands_text()}."
        )

    def director_line(self) -> str:
        return (
            f"Entry window: {self.window} (account profile `{self.account_profile}`), fixed "
            "by config for every menu the Quant prices; it may not be narrowed, widened or "
            "restated as a rule per call. Expiry buckets in the portfolio block measure "
            "concentration only; to ease an expiry cluster, spread expiries within the "
            "entry window."
        )

    def risk_line(self) -> str:
        return (
            f"Entry window: every proposed structure is inside the {self.window} window "
            f"fixed by config (account profile `{self.account_profile}`; "
            f"{self.bands_text()}); do not decline or downsize a structure for its DTE alone."
        )


def entry_terms(settings: ArcSettings) -> EntryTerms:
    """The entry window and the delta bands of the active profile's strategies."""
    lo, hi = settings.entry_dte_window
    st = settings.profile.stance_strategies
    names = {*st.bullish, *st.bearish, *st.neutral}
    bands: list[DeltaBand] = []
    if names & _CREDIT:
        bands.append(
            DeltaBand(
                kind="credit_short",
                lo=settings.scanner_short_delta_min,
                hi=settings.scanner_short_delta_max,
            )
        )
    if names & _DEBIT_VERTICAL:
        bands.append(
            DeltaBand(
                kind="debit_short",
                lo=settings.scanner_debit_short_delta_min,
                hi=settings.scanner_debit_short_delta_max,
            )
        )
    if names & (_DEBIT_VERTICAL | _LONG_SINGLE):
        bands.append(
            DeltaBand(
                kind="long", lo=settings.scanner_long_delta_min, hi=settings.scanner_long_delta_max
            )
        )
    return EntryTerms(account_profile=settings.account_profile, dte_min=lo, dte_max=hi, bands=bands)


_DTE_TEXT = re.compile(r"\bDTE\b|days?[ -]to[ -]expir", re.IGNORECASE)
# A DTE / delta range a persona wrote into prose ("N-M DTE", "inside N-M",
# "hold N-M days", "N-M delta band", "past the N-M target"). Dates (4-digit
# years) and strike pairs ("240/225") never match. No literal range is written
# in arc/personas (tests/test_e34a_entry_window.py greps for it).
_RANGE = r"\b\d{1,3}\s*[-\u2013]\s*\d{1,3}\b"
_CARRIED_RANGE = re.compile(
    rf"(?:(?<=inside )|(?<=outside )|(?<=within )|(?<=past )|(?<=in the )|(?<=inside the )"
    rf"|(?<=outside the )|(?<=within the )|(?<=past the )){_RANGE}"
    rf"|{_RANGE}(?=\s*(?:DTE\b|days?\b|delta\b|band\b|window\b|target\b|range\b))",
    re.IGNORECASE,
)
SCRUBBED_RANGE = "configured-range"


def mentions_dte(text: str) -> bool:
    """True when a persona reason cites DTE / days to expiry (observability, E3.4a)."""
    return bool(_DTE_TEXT.search(text))


def scrub_carried_text(text: str) -> str:
    """Replace persona-written DTE / delta ranges in carried-over prose.

    Earlier persona output (notes, the shortlist, Quant structures, Sweep
    rationales) is context, not instructions: a window a persona improvised
    ("prefer N-M DTE", "short strikes at N-M delta") must not reach the next
    prompt as a rule, so each such range is rewritten to ``configured-range``
    before the text is shown again. The configured window and bands are stated
    once, by :class:`EntryTerms`.
    """
    return _CARRIED_RANGE.sub(SCRUBBED_RANGE, text)
