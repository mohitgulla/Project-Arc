"""LLM backends for the Scalp persona.

The Scalp runs on the cheap model tier through Hermes (PLAN §2.4, D8); the
model for each persona comes from ``config/llm_routing.yaml`` via
:mod:`arc.llm_routing`. ``HermesScalpLLM`` shells out to ``hermes -z`` (one-shot mode) with a
pinned model/provider, no project rules and a minimal toolset, so the
persona has no broker access and no repository context.

``FixtureScalpLLM`` replays canned responses for dry-run mode and tests;
it never touches the network.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from arc.config import ArcSettings

log = structlog.get_logger()

FIXTURE_MODEL = "fixture"

# Hermes needs at least one valid toolset; ``todo`` is inert (no network,
# no filesystem, no broker) and is the smallest one available.
_HERMES_TOOLSET = "todo"

# Kanban worker env must not leak into the persona call, otherwise the
# child session would believe it is working a board card.
_STRIPPED_ENV_PREFIXES = ("HERMES_KANBAN_",)


class ScalpLLMError(RuntimeError):
    """The Scalp LLM call failed (transport, timeout, non-zero exit)."""


@dataclass(frozen=True)
class LLMResult:
    """Raw text returned by a Scalp LLM call plus the model that produced it.

    Usage fields come from ``hermes -z --usage-file`` and are ``None`` when
    unknown (fixtures). ``cost_usd`` is 0 on a subscription ("included") plan;
    the token counts are still reported.
    """

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class PersonaLLM(Protocol):
    """Anything that turns a persona prompt (Scalp, Scout briefs) into raw response text."""

    def complete(self, prompt: str) -> LLMResult: ...


# ---------------------------------------------------------------------------
# Fixture backend (dry-run / tests)
# ---------------------------------------------------------------------------


_EMPTY_RESPONSE = json.dumps({"candidates": [], "scan_summary": "fixture responses exhausted"})


@dataclass
class FixtureScalpLLM:
    """Replays canned responses in order; returns an empty scan once exhausted.

    Thread-safe (D63: the loop's exit and open branches may share one persona's
    fixture); the order of two concurrent calls is the order they arrive in.
    """

    responses: Sequence[str]
    prompts: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def from_dir(cls, path: Path) -> FixtureScalpLLM:
        """Load ``*.txt`` responses from *path*, sorted by filename."""
        return cls([p.read_text() for p in sorted(path.glob("*.txt"))])

    def complete(self, prompt: str) -> LLMResult:
        with self._lock:
            idx = len(self.prompts)
            self.prompts.append(prompt)
        text = self.responses[idx] if idx < len(self.responses) else _EMPTY_RESPONSE
        return LLMResult(text=text, model=FIXTURE_MODEL)


# ---------------------------------------------------------------------------
# Hermes backend (cheap tier)
# ---------------------------------------------------------------------------


def _child_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not any(k.startswith(p) for p in _STRIPPED_ENV_PREFIXES)
    }


@dataclass
class HermesScalpLLM:
    """Run the Scalp prompt through ``hermes -z`` on the cheap model tier."""

    model: str
    provider: str
    hermes_bin: str = "hermes"
    timeout_seconds: int = 240
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    @classmethod
    def from_settings(
        cls,
        settings: ArcSettings,
        persona: str = "scalp",
        *,
        timeout_seconds: int | None = None,
    ) -> HermesScalpLLM:
        """Backend for *persona*; its model comes from ``config/llm_routing.yaml`` (E8.1)."""
        from arc.llm_routing import resolve

        route = resolve(persona, settings)
        return cls(
            model=route.model,
            provider=route.provider,
            hermes_bin=settings.scalp_hermes_bin,
            timeout_seconds=timeout_seconds or settings.scalp_timeout_seconds,
        )

    def command(self, prompt: str, usage_file: Path) -> list[str]:
        return [
            self.hermes_bin,
            "-z",
            prompt,
            "-m",
            self.model,
            "--provider",
            self.provider,
            "-t",
            _HERMES_TOOLSET,
            "--ignore-rules",
            "--usage-file",
            str(usage_file),
        ]

    def complete(self, prompt: str) -> LLMResult:
        with tempfile.TemporaryDirectory(prefix="arc-scalp-") as tmp:
            usage_file = Path(tmp) / "usage.json"
            try:
                proc = self.runner(
                    self.command(prompt, usage_file),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    cwd=tmp,  # no AGENTS.md / repo context for the persona
                    env=_child_env(),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ScalpLLMError(f"hermes call failed: {exc}") from exc

            usage = _read_usage(usage_file)

        if proc.returncode != 0 or usage.get("failed"):
            err = (proc.stderr or "").strip()[-500:]
            raise ScalpLLMError(f"hermes exited {proc.returncode}: {err}")

        model = str(usage.get("model") or self.model)
        log.info(
            "scalp.llm.done",
            model=model,
            cost_usd=usage.get("estimated_cost_usd"),
            total_tokens=usage.get("total_tokens"),
        )
        return LLMResult(
            text=proc.stdout,
            model=model,
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cost_usd=_cost(usage),
        )


def _int(v: object) -> int | None:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _cost(usage: dict[str, object]) -> float | None:
    if usage.get("cost_status") == "included":
        return 0.0
    v = usage.get("estimated_cost_usd")
    if isinstance(v, int | float | str) and not isinstance(v, bool):
        try:
            return max(float(v), 0.0)
        except ValueError:
            return None
    return None


def _read_usage(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
