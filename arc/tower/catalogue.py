"""E13.14 (D56): the persona and category catalogue served by ``GET /api/meta``.

Every persona / category label the SPA shows comes from here, built once per read
from ``config/routines.yaml`` (via :class:`~arc.routines.config.RoutinesConfig`) and
the emoji map in :mod:`arc.slack.personas` (the single emoji source, E13.13). The
SPA never hard-codes a persona name.

* **Personas** are the :data:`~arc.routines.config.TIMELINE_PERSONAS` chips. The
  label is the persona's name (``Persona`` value; for ``monitor``, which is not a
  Slack persona, the ``monitor`` job's ``label:``); ``group`` comes from the first
  job that declares ``persona: <key>``, and ``llm`` is true when any such job or a
  chain step named ``<key>.*`` (``steps:``) calls a model.
* **Categories** are the six D56 :class:`~arc.context.categories.SourceCategory`
  values in display order (``categories.<c>.label/weight/max_age``) plus one
  ``reference`` entry (reference data, D56: not a category, no share).

Pure functions over config; no store reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field

from arc.context.categories import CATEGORY_ORDER, REFERENCE, SourceCategory
from arc.routines.config import TIMELINE_PERSONAS
from arc.slack.personas import PERSONA_EMOJI, persona_for

if TYPE_CHECKING:
    from arc.routines.config import RoutinesConfig

__all__ = [
    "REFERENCE_LABEL",
    "CategoryMeta",
    "Feed",
    "PersonaKey",
    "PersonaMeta",
    "category_catalogue",
    "category_feed",
    "persona_catalogue",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)

PersonaKey = Literal["scout", "scalp", "research", "quant", "risk", "broker", "ops", "monitor"]
Feed = Literal["scalp", "scout"]

#: D56: the Sources page / Session Timeline group for reference-data sources.
REFERENCE_LABEL = "Reference data"

#: D56 (owner decision, categories): the Scalp's 30-min feed reads these; the rest
#: (options_slow, youtube_macro, youtube_micro, D58 retail_buzz) are the Scout's daily feed.
_SCALP_FEED: frozenset[SourceCategory] = frozenset(
    {SourceCategory.MARKET_NEWS, SourceCategory.COMPANY_DATA, SourceCategory.OPTIONS_FAST}
)


class PersonaMeta(BaseModel):
    """One persona chip: key, display label, emoji, whether it calls an LLM, its group."""

    model_config = _STRICT

    key: PersonaKey
    label: str = Field(description="Display name, e.g. 'Research'")
    emoji: str = Field(description="arc.slack.personas PERSONA_EMOJI; '' when none (monitor)")
    llm: bool = Field(description="Any job with this persona calls a model")
    group: str = Field(description="Timeline group (TIMELINE_GROUPS key) of its first job")


class CategoryMeta(BaseModel):
    """One D56 source category (or the reference-data group)."""

    model_config = _STRICT

    key: str
    label: str
    max_age_minutes: int = Field(description="Freshness window; 0 for reference data")
    weight: float = Field(description="categories.<c>.weight; 0 for reference data")
    feed: Feed = Field(description="scalp = 30-min fast feed; scout = daily slow feed")
    reference: bool = False


def category_feed(category: SourceCategory) -> Feed:
    """The persona feed that reads *category* (D56 owner decision)."""
    return "scalp" if category in _SCALP_FEED else "scout"


def persona_catalogue(routines: RoutinesConfig) -> list[PersonaMeta]:
    """Every timeline persona, in :data:`TIMELINE_PERSONAS` order."""
    keys = get_args(PersonaKey)
    out: list[PersonaMeta] = []
    for key in TIMELINE_PERSONAS:
        if key not in keys:  # pragma: no cover - TIMELINE_PERSONAS and PersonaKey agree
            continue
        jobs = [
            (name, spec)
            for name, spec in routines.personas.items()
            if spec.options.get("persona") == key
        ]
        slack = persona_for(key)
        if slack is not None:
            label, emoji = slack.value, PERSONA_EMOJI[slack]
        else:
            own = routines.personas.get(key)
            label = str(own.options.get("label")) if own and own.options.get("label") else key
            label, emoji = (label[:1].upper() + label[1:]), ""
        # chain-only steps named after the persona (quant.open, risk.exit) count too
        steps = [s for n, s in routines.steps.items() if n.partition(".")[0] == key]
        llm = any((s.llm if s.llm is not None else True) for s in [*(j for _, j in jobs), *steps])
        if jobs:
            group = str(jobs[0][1].options.get("group") or "other")
        elif steps and routines.loop.job in routines.personas:  # chain-only (Risk): the loop
            group = str(routines.personas[routines.loop.job].options.get("group") or "other")
        else:
            group = "other"
        out.append(
            PersonaMeta(key=cast("PersonaKey", key), label=label, emoji=emoji, llm=llm, group=group)
        )
    return out


def category_catalogue(routines: RoutinesConfig) -> list[CategoryMeta]:
    """The seven categories (D56 six + D58 retail_buzz) in display order, then the
    reference-data group."""
    out: list[CategoryMeta] = []
    for cat in CATEGORY_ORDER:
        spec = routines.category_spec(cat)
        dur = spec.max_age.duration
        minutes = int(dur.total_seconds() // 60) if dur is not None else 0
        out.append(
            CategoryMeta(
                key=cat.value,
                label=spec.label,
                max_age_minutes=minutes,
                weight=spec.weight,
                feed=category_feed(cat),
            )
        )
    out.append(
        CategoryMeta(
            key=REFERENCE,
            label=REFERENCE_LABEL,
            max_age_minutes=0,
            weight=0.0,
            feed="scout",
            reference=True,
        )
    )
    return out
