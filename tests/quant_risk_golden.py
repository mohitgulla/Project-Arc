"""E13.9 golden helper: the offline fixture chain's Quant and Risk prompts, hashed.

Imported by tests/test_quant_risk_loop.py and run stand-alone against an origin/main
checkout (``python tests/quant_risk_golden.py``) to pin the flag-off prompt hashes.
Uses only APIs that exist on main before E13.9 (``fixture_run``, ``PipelineEnv``).
"""

from __future__ import annotations

import hashlib
import logging
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


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


if __name__ == "__main__":
    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    for persona, prompts in fixture_prompts().items():
        for i, p in enumerate(prompts):
            print(persona, i, digest(p), len(p))
