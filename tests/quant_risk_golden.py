"""E13.9 golden helper: the offline fixture chain's Quant and Risk prompts, hashed.

Imported by tests/test_quant_risk_loop.py and run stand-alone against an origin/main
checkout (``python tests/quant_risk_golden.py``) to pin the flag-off prompt hashes.
Uses only APIs that exist on main before E13.9 (``fixture_run``, ``PipelineEnv``).
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any


def fixture_prompts(routines: Any = None) -> dict[str, list[str]]:
    """Run ``arc propose --fixtures`` offline; the prompts each persona LLM received."""
    from arc.config import ArcSettings
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline import FIXTURE_NOW, PipelineEnv
    from arc.pipeline.runner import open_db, run_propose
    from arc.routines.config import load_routines
    from arc.routines.heartbeat import RecordingNotifier

    settings = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
    env = PipelineEnv.fixtures()
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    run_propose(
        conn,
        settings,
        routines if routines is not None else load_routines(),
        env,
        now=FIXTURE_NOW,
        notifier=RecordingNotifier(),
    )
    return {p: list(env.llms[p].prompts) for p in ("research", "quant", "risk")}  # type: ignore[attr-defined]


_FLOAT = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")


def digest(text: str) -> str:
    """sha256 of *text* with every decimal rounded to 6 significant digits.

    The prompts carry computed greeks/PoP/EV; their last digits differ between the
    macOS and Linux libm, so the raw bytes are platform-dependent. Rounding keeps any
    real change (a moved strike, a new field, reworded text) while dropping ulp noise.
    """
    norm = _FLOAT.sub(lambda m: f"{float(m.group()):.6g}", text)
    return hashlib.sha256(norm.encode()).hexdigest()


if __name__ == "__main__":
    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    for persona, prompts in fixture_prompts().items():
        for i, p in enumerate(prompts):
            print(persona, i, digest(p), len(p))
