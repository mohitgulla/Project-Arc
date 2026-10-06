"""Config models for the trending tier (E12.3, ``config/routines.yaml`` →
``sources.universe.trending.trending``).

A leaf module (pydantic only) so ``arc.routines.config`` can validate the block at
load without importing the scoring code, the symbol master or any HTTP client (the
read-only Tower imports routines config; import-linter contract).
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves the annotation at runtime
import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["INPUT_TYPES", "InputType", "TrendingConfig", "TrendingError", "TrendingInput"]

InputType = Literal["news", "apewisdom", "stocktwits", "scalp"]
INPUT_TYPES: tuple[InputType, ...] = ("news", "apewisdom", "stocktwits", "scalp")
_NETWORK: frozenset[str] = frozenset({"apewisdom", "stocktwits"})


class TrendingError(RuntimeError):
    """The tier cannot be built (no symbol master, too few live inputs): nothing is
    written; yesterday's entry has already expired, so the tier is empty today."""


def _duration(v: Any) -> Any:
    if isinstance(v, str):
        from arc.context.ttl import parse_duration

        return parse_duration(v)
    return v


class TrendingInput(BaseModel):
    """One ranking input. ``type`` picks the adapter; everything else is a knob."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: InputType
    enabled: bool = True
    label: str = ""
    # news / scalp: how many sessions back (today, when a session, counts as one)
    lookback_sessions: int = Field(3, ge=1, le=20)
    # news / scalp: the input is stale (contributes nothing) when its newest row is
    # older than this. Network inputs are fetched live, so it does not apply to them.
    max_age: _dt.timedelta | None = None
    # apewisdom / stocktwits: pages fetched (every page must answer, else the input fails)
    urls: list[str] = Field(default_factory=list)
    # news: raw_docs.source values never counted (the earnings calendar is not news)
    exclude_sources: list[str] = Field(default_factory=list)

    @field_validator("max_age", mode="before")
    @classmethod
    def _max_age(cls, v: Any) -> Any:
        return _duration(v)

    @model_validator(mode="after")
    def _urls(self) -> TrendingInput:
        if self.type in _NETWORK and not self.urls:
            msg = f"trending input of type {self.type!r} needs urls"
            raise ValueError(msg)
        if self.type not in _NETWORK and self.urls:
            msg = f"trending input of type {self.type!r} takes no urls"
            raise ValueError(msg)
        return self


class TrendingConfig(BaseModel):
    """The ``trending:`` block of the ``universe.trending`` job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pool: int = Field(40, ge=1, le=200)  # top N by score that get the liquidity screen
    min_inputs: int = Field(2, ge=1)
    exclude_market_reference: bool = True
    timeout_s: float = Field(15.0, gt=0)
    retries: int = Field(1, ge=0, le=5)
    inputs: dict[str, TrendingInput] = Field(min_length=1)

    @field_validator("inputs")
    @classmethod
    def _names(cls, v: dict[str, TrendingInput]) -> dict[str, TrendingInput]:
        for name in v:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                msg = f"trending input name {name!r}: lower-case letters, digits, _"
                raise ValueError(msg)
        return v

    @property
    def enabled(self) -> dict[str, TrendingInput]:
        return {k: s for k, s in self.inputs.items() if s.enabled}

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> TrendingConfig:
        raw = options.get("trending")
        if not isinstance(raw, dict):
            msg = "universe.trending: missing `trending:` block (pool, min_inputs, inputs)"
            raise TrendingError(msg)
        return cls.model_validate(raw)
