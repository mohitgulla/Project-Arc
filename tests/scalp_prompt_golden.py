"""E13.10: the stage-2 Scalp prompt inputs pinned by tests/test_scalp_prompt_snapshot.py.

Run stand-alone against an origin/main checkout to (re)write the golden file::

    python -m tests.scalp_prompt_golden > tests/fixtures/scalp/stage2_prompt_flag_off.txt

The golden was written from main at dac00d1 (before E13.10), so the flag-off prompt
is proven byte-identical to the pre-card prompt.
"""

from __future__ import annotations

import sys

from arc.config import ArcSettings
from arc.context.kinds import StoryEvidence, StoryPayload
from arc.models import CatalystType

DAY = "2026-10-06"


def digests() -> list[StoryPayload]:
    return [
        StoryPayload(
            story_id="st-1",
            headline="NVDA beats on data-center revenue",
            category="company_data",
            source_keys=["rss:cnbc_earnings", "edgar"],
            distinct_sources=2,
            doc_ids=["d1", "d2"],
            urls=["https://example.com/nvda-1", "https://sec.gov/nvda-8k"],
            first_published="2026-10-06T08:00:00-04:00",
            last_published="2026-10-06T09:10:00-04:00",
            tickers=["NVDA"],
            summary="NVDA reported data-center revenue above guidance.",
            catalyst_type=CatalystType.EARNINGS,
            catalyst_date="2026-10-06",
            evidence=[
                StoryEvidence(url="https://example.com/nvda-1", quote="revenue above guidance")
            ],
            mode="llm",
        ),
        StoryPayload(
            story_id="st-2",
            headline="Fed minutes signal patience",
            category="market_news",
            source_keys=["rss:fed"],
            distinct_sources=1,
            doc_ids=["d3"],
            urls=["https://federalreserve.gov/m"],
            first_published="2026-10-06T07:00:00-04:00",
            last_published="2026-10-06T07:00:00-04:00",
            tickers=[],
            summary="Fed minutes signal patience.",
            mode="extractive",
        ),
    ]


def settings() -> ArcSettings:
    return ArcSettings(env="paper", scalp_min_confidence=0.6)


def prompt() -> str:
    from arc.ingest.scalp import build_stage2_prompt

    return build_stage2_prompt(
        digests(),
        settings(),
        DAY,
        open_universe=True,
        ticker_facts="",
        universe=["NVDA", "AAPL", "SPY"],
    )


if __name__ == "__main__":
    sys.stdout.write(prompt())
