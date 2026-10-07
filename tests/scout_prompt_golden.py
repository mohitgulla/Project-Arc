"""E13.20: the Scout prompt inputs pinned by tests/test_scout_retail_buzz.py.

Re-write the golden files after an intended prompt change::

    python -m tests.scout_prompt_golden with > tests/fixtures/scout/prompt_with_retail_buzz.txt
    python -m tests.scout_prompt_golden without > tests/fixtures/scout/prompt_no_retail_buzz.txt
"""

from __future__ import annotations

import sys
from typing import Any

from arc.context.kinds import RetailBuzzPayload
from arc.personas.scout import ScoutBrief, ScoutInput, build_scout_prompt, retail_buzz_view

AS_OF = "2026-10-08T06:00:00-04:00"
TRENDING = ["GME", "RKLB", "SOFI"]


def buzz() -> RetailBuzzPayload:
    """Reddit + Stocktwits rows: GME/RKLB/SOFI in both, BTC.X crypto, a single-input tail."""
    reddit = [
        ("GME", 1, 1234.0, 3),
        ("RKLB", 2, 640.0, 9),
        ("TSLA", 3, 610.0, 2),
        ("SOFI", 4, 300.0, 4),
        ("ACHR", 5, 120.0, None),
    ]
    stocktwits = [
        ("RKLB", 1, 33.1, "NASDAQ", "US"),
        ("BTC.X", 2, 30.0, "CRYPTO", "US"),
        ("GME", 3, 25.4, "NYSE", "US"),
        ("SOFI", 4, 20.0, "NASDAQ", "US"),
        ("OKLO", 5, 18.0, "NYSE", "US"),
    ]
    return RetailBuzzPayload.model_validate(
        {
            "as_of": "2026-10-08T05:40:00-04:00",
            "session": "2026-10-08",
            "inputs": {
                "reddit": {
                    "type": "apewisdom",
                    "status": "ok",
                    "fetched_at": "2026-10-08T05:40:01-04:00",
                    "rows": [
                        {
                            "symbol": s,
                            "position": r,
                            "rank": r,
                            "mentions": m,
                            "rank_24h_ago": prev,
                        }
                        for s, r, m, prev in reddit
                    ],
                },
                "stocktwits": {
                    "type": "stocktwits",
                    "status": "ok",
                    "fetched_at": "2026-10-08T05:40:02-04:00",
                    "rows": [
                        {
                            "symbol": s,
                            "position": p,
                            "trending_score": sc,
                            "exchange": ex,
                            "region": rg,
                        }
                        for s, p, sc, ex, rg in stocktwits
                    ],
                },
            },
        }
    )


def scout_input(*, with_buzz: bool) -> ScoutInput:
    extra: dict[str, Any] = (
        {"retail_buzz": retail_buzz_view(buzz().model_dump(), trending=TRENDING)}
        if with_buzz
        else {}
    )
    return ScoutInput(
        session="2026-10-08",
        as_of=AS_OF,
        max_discovery=25,
        discovery_floor=0.6,
        budget_chars=12_000,
        presence={
            "youtube_macro": "YouTube macro briefs: 1/2 channels (missing: Bravos)",
            "youtube_micro": "YouTube micro briefs: 1/3 channels (missing: TradeBrigade, Arete)",
        },
        present={"youtube_macro": ["fxevolution"], "youtube_micro": ["stockedup"]},
        missing={"youtube_macro": ["Bravos"], "youtube_micro": ["TradeBrigade", "Arete"]},
        configured={"youtube_macro": 2, "youtube_micro": 3},
        briefs=[
            ScoutBrief(
                origin="youtube:fxevolution",
                channel="FX Evolution",
                category="youtube_macro",
                text='{"title": "Dollar rolls over"}',
            ),
            ScoutBrief(
                origin="youtube:stockedup",
                channel="StockedUp",
                category="youtube_micro",
                text='{"title": "RKLB breakout"}',
            ),
        ],
        options_slow={
            "options_daily": {
                "as_of": "2026-10-07",
                "ratios": [{"segment": "total", "ratio": 0.91}],
                "open_interest": [],
            },
            "vx_curve": None,
            "vol_term": None,
        },
        options_as_of={"options_daily": "2026-10-07", "vx_curve": None, "vol_term": None},
        higher_tier=["AAPL", "NVDA"],
        **extra,
    )


def prompt(*, with_buzz: bool) -> str:
    return build_scout_prompt(scout_input(with_buzz=with_buzz))


if __name__ == "__main__":  # pragma: no cover - golden writer
    sys.stdout.write(prompt(with_buzz=sys.argv[1:] == ["with"]))
