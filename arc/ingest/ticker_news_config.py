"""Config models for the ``ticker_news`` source (E14.1, D60).

``config/routines.yaml`` → ``sources.ticker_news.inputs``: one entry per input. ``type``
picks the fetcher (``alpaca_news`` = Alpaca ``/v1beta1/news`` (Benzinga), chunked by
symbol; ``finnhub_company_news`` = Finnhub ``/company-news``, one call per ticker
through the shared cross-process Finnhub budget). ``enabled_when`` decides when an
input runs: ``always`` (a primary) or ``primary_failed`` (a fallback, only for the
tickers every primary failed on this run).

A leaf module (pydantic only) so :mod:`arc.routines.config` can validate the block at
load without importing an HTTP client (the read-only Tower imports routines config).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ALPACA_NEWS_URL",
    "INPUT_TYPES",
    "EnabledWhen",
    "InputType",
    "TickerNewsConfig",
    "TickerNewsInputSpec",
]

InputType = Literal["alpaca_news", "finnhub_company_news"]
INPUT_TYPES: tuple[InputType, ...] = ("alpaca_news", "finnhub_company_news")
EnabledWhen = Literal["always", "primary_failed"]
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"


class TickerNewsInputSpec(BaseModel):
    """One input: which provider, when it runs and how hard it tries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: InputType
    enabled: bool = True
    enabled_when: EnabledWhen = "always"
    label: str = ""
    url: str = ALPACA_NEWS_URL  # alpaca_news only
    # alpaca_news: symbols per call (Alpaca's own cap is 50 per request we use)
    chunk_size: int = Field(50, ge=1, le=50)
    page_limit: int = Field(50, ge=1, le=50)  # articles per page (Alpaca max 50)
    # a chunk whose walk needs more pages than this fails (cursor not advanced)
    max_pages: int = Field(20, ge=1, le=200)
    timeout_s: float = Field(15.0, gt=0, le=120)
    retry_after_default_s: float = Field(30.0, ge=0, le=300)  # 429 without Retry-After


class TickerNewsConfig(BaseModel):
    """The ``inputs:`` block of the ``ticker_news`` source job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inputs: dict[str, TickerNewsInputSpec] = Field(min_length=1)

    @field_validator("inputs")
    @classmethod
    def _names(cls, v: dict[str, TickerNewsInputSpec]) -> dict[str, TickerNewsInputSpec]:
        for name in v:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                msg = f"ticker_news input name {name!r}: lower-case letters, digits, _"
                raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _one_primary(self) -> TickerNewsConfig:
        if not self.primaries:
            msg = "ticker_news: at least one enabled input needs enabled_when: always"
            raise ValueError(msg)
        return self

    @property
    def enabled(self) -> dict[str, TickerNewsInputSpec]:
        return {k: s for k, s in self.inputs.items() if s.enabled}

    @property
    def primaries(self) -> dict[str, TickerNewsInputSpec]:
        return {k: s for k, s in self.enabled.items() if s.enabled_when == "always"}

    @property
    def fallbacks(self) -> dict[str, TickerNewsInputSpec]:
        return {k: s for k, s in self.enabled.items() if s.enabled_when == "primary_failed"}

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> TickerNewsConfig:
        """From the job's options (``inputs:``); a missing block raises ``ValueError``."""
        return cls.model_validate({"inputs": options.get("inputs") or {}})
