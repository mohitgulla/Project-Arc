"""Config models for the ``retail_buzz`` source (E13.19, D58).

``config/routines.yaml`` → ``sources.retail_buzz.inputs``: one entry per input
(Reddit via ApeWisdom, Stocktwits). Adding or removing an input is config only;
``type`` picks the parser.

A leaf module (pydantic only) so :mod:`arc.routines.config` can validate the block at
load without importing an HTTP client (the read-only Tower imports routines config).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["INPUT_TYPES", "InputType", "RetailBuzzConfig", "RetailBuzzInputSpec"]

InputType = Literal["apewisdom", "stocktwits"]
INPUT_TYPES: tuple[InputType, ...] = ("apewisdom", "stocktwits")


class RetailBuzzInputSpec(BaseModel):
    """One input: where to fetch it and how hard to try."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: InputType
    enabled: bool = True
    label: str = ""
    # every page must answer, else the input fails (contributes nothing today)
    urls: list[str] = Field(min_length=1)
    timeout_s: float = Field(15.0, gt=0, le=120)
    retries: int = Field(1, ge=0, le=5)  # connection errors / timeouts / 5xx only


class RetailBuzzConfig(BaseModel):
    """The ``inputs:`` block of the ``retail_buzz`` source job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inputs: dict[str, RetailBuzzInputSpec] = Field(min_length=1)

    @field_validator("inputs")
    @classmethod
    def _names(cls, v: dict[str, RetailBuzzInputSpec]) -> dict[str, RetailBuzzInputSpec]:
        for name in v:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                msg = f"retail_buzz input name {name!r}: lower-case letters, digits, _"
                raise ValueError(msg)
        return v

    @property
    def enabled(self) -> dict[str, RetailBuzzInputSpec]:
        return {k: s for k, s in self.inputs.items() if s.enabled}

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> RetailBuzzConfig:
        """From the job's options (``inputs:``); a missing block raises ``ValueError``."""
        return cls.model_validate({"inputs": options.get("inputs") or {}})
