"""Config model for the ``retail_sentiment`` source (E14.6, D60).

``config/routines.yaml`` → ``sources.retail_sentiment``: the Stocktwits per-symbol
stream, one request per ticker in scope (active list ∪ open underlyings), paced.

A leaf module (pydantic only) so :mod:`arc.routines.config` can validate the block at
load without importing an HTTP client (the read-only Tower imports routines config).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["DEFAULT_STREAM_URL", "RetailSentimentConfig"]

DEFAULT_STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"

# Job options that belong to the scheduler / registry, not to this model.
_JOB_KEYS = frozenset({"label", "about", "category", "feed", "group", "max_age", "reference"})


class RetailSentimentConfig(BaseModel):
    """Knobs of the ``retail_sentiment`` source job (all optional; defaults below)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = DEFAULT_STREAM_URL
    # `active_list` (active list ∪ open underlyings) or an explicit list (tests, probes)
    tickers: str | list[str] = "active_list"
    max_tickers: int = Field(55, ge=1, le=200)
    pace_s: float = Field(1.5, ge=0, le=30)  # seconds between two requests
    pages: int = Field(1, ge=1, le=2)  # 2 = one `max` cursor page when tagged < min_tagged
    min_tagged: int = Field(5, ge=1, le=30)  # fewer tags -> bull_ratio None ("too few tags")
    # hard cap on requests per run, under Stocktwits' unauthenticated limit
    max_requests: int = Field(150, ge=1, le=400)
    timeout_s: float = Field(15.0, gt=0, le=120)
    retries: int = Field(1, ge=0, le=5)  # connection errors / timeouts / 5xx only

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if "{ticker}" not in v:
            msg = "retail_sentiment.url must contain {ticker}"
            raise ValueError(msg)
        return v

    @field_validator("tickers")
    @classmethod
    def _tickers(cls, v: str | list[str]) -> str | list[str]:
        if isinstance(v, str) and v != "active_list":
            msg = f"retail_sentiment.tickers: active_list or a list, got {v!r}"
            raise ValueError(msg)
        return v

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> RetailSentimentConfig:
        """From the job's options (scheduler keys ignored); unknown keys raise."""
        return cls.model_validate({k: v for k, v in options.items() if k not in _JOB_KEYS})
