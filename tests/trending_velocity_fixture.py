"""E14.5: a deterministic retail_buzz fixture for the trending ranker parity test.

``tests/fixtures/trending/parity_rank_gain.json`` is the ranker's output on this fixture
**before** E14.5 (``scoring`` did not exist). Re-write it only for an intended change to
the ``rank_gain`` ranking::

    python -m tests.trending_velocity_fixture tests/fixtures/trending/parity_rank_gain.json
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any

from arc.context.kinds import RetailBuzzPayload
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 10, 8, 5, 50, tzinfo=ET)

#: (symbol, mentions, rank_24h_ago, mentions_24h_ago) in ApeWisdom rank order.
REDDIT: list[tuple[str, float, int | None, float | None]] = [
    ("MU", 383, 2, 247),
    ("SPY", 300, 1, 330),
    ("APLD", 157, 20, 30),
    ("BULL", 126, 90, 3),
    ("NVDA", 82, 3, 123),
    ("TSLA", 80, 4, 95),
    ("SOXL", 70, 8, 20),
    ("GME", 60, 5, 58),
    ("AMD", 55, 6, 70),
    ("PLTR", 50, 7, 49),
    ("RKLB", 44, 15, 11),
    ("SOFI", 40, 9, 41),
    ("HOOD", 35, 10, 36),
    ("OPEN", 30, 30, 6),
    ("ACHR", 28, 11, 33),
    ("IONQ", 25, 12, None),
    ("RGTI", 22, 40, 2),
    ("AAPL", 20, 13, 22),
    ("META", 18, 14, 25),
    ("AMZN", 16, 16, 15),
    ("OKLO", 14, None, None),
    ("SMR", 12, 50, 1),
    ("LCID", 9, 17, 2),
    ("NIO", 8, 18, 9),
    ("RIVN", 7, 19, 1),
]
#: Stocktwits trending (symbol, trending_score, exchange).
STOCKTWITS: list[tuple[str, float, str]] = [
    ("RGTI", 30.0, "NASDAQ"),
    ("BTC.X", 29.0, "CRYPTO"),
    ("APLD", 27.0, "NASDAQ"),
    ("GME", 25.0, "NYSE"),
    ("OPEN", 22.0, "NASDAQ"),
    ("MU", 20.0, "NASDAQ"),
    ("SMR", 18.0, "NYSE"),
    ("CRWV", 16.0, "NASDAQ"),
    ("NVDA", 14.0, "NASDAQ"),
    ("BYND", 12.0, "NASDAQ"),
]
LEVERAGED = {"SOXL": "Direxion Daily Semiconductor Bull 3X Shares"}


def master() -> SymbolMaster:
    syms = {s for s, *_ in REDDIT} | {s for s, _, ex in STOCKTWITS if ex != "CRYPTO"}
    return SymbolMaster(
        fetched_at=NOW,
        symbols={
            s: SymbolInfo(
                symbol=s,
                name=LEVERAGED.get(s, f"{s} Inc"),
                exchange="NASDAQ",
                options=True,
                tradable=True,
                sources=["sec", "alpaca"],
            )
            for s in sorted(syms)
        },
    )


def buzz(*, with_counts: bool = True) -> RetailBuzzPayload:
    """The fixture payload; ``with_counts=False`` leaves out ``mentions_24h_ago``
    (the pre-E14.5 row shape)."""

    def reddit_row(i: int, s: str, m: float, prev: int | None, m24: float | None) -> dict:
        row: dict[str, Any] = {
            "symbol": s,
            "position": i,
            "rank": i,
            "name": LEVERAGED.get(s, f"{s} Inc"),
            "mentions": m,
            "rank_24h_ago": prev,
        }
        if with_counts:
            row |= {"mentions_24h_ago": m24, "upvotes": m * 3}
        return row

    return RetailBuzzPayload.model_validate(
        {
            "as_of": NOW.isoformat(),
            "session": NOW.date().isoformat(),
            "inputs": {
                "reddit": {
                    "type": "apewisdom",
                    "status": "ok",
                    "fetched_at": NOW.isoformat(),
                    "urls": ["https://ape/1"],
                    "rows": [
                        reddit_row(i, s, m, prev, m24)
                        for i, (s, m, prev, m24) in enumerate(REDDIT, 1)
                    ],
                },
                "stocktwits": {
                    "type": "stocktwits",
                    "status": "ok",
                    "fetched_at": NOW.isoformat(),
                    "urls": ["https://st/1"],
                    "rows": [
                        {
                            "symbol": s,
                            "position": i,
                            "rank": i,
                            "trending_score": sc,
                            "exchange": ex,
                            "region": "US",
                        }
                        for i, (s, sc, ex) in enumerate(STOCKTWITS, 1)
                    ],
                },
            },
        }
    )


def ranked(opts: Any = None, *, with_counts: bool = True) -> dict[str, Any]:
    """The tier payload + score table for the fixture (JSON-friendly)."""
    from arc.universe.trending import TrendingOptions, build_payload, run_trending, table

    res = run_trending(
        buzz(with_counts=with_counts),
        enabled=["reddit", "stocktwits"],
        stale=False,
        opts=opts or TrendingOptions(),
        now=NOW,
        master=master(),
        size=12,
        exclude={"SPY": "market_reference", "AAPL": "core"},
        screen=None,
    )
    payload = build_payload(res, now=NOW).model_dump(mode="json")
    # D64 (E14.7) added fields (scores, runs, merge audit) postdate the parity file; the
    # parity guards the pre-existing ranking fields byte for byte.
    for k in D64_PAYLOAD_KEYS:
        payload.pop(k, None)
    for m in payload["members"]:
        for k in D64_MEMBER_KEYS:
            m.pop(k, None)
    return {"payload": payload, "table": table(res)}


D64_MEMBER_KEYS = ("score", "score_today", "score_prev", "runs", "stance", "origins")
D64_PAYLOAD_KEYS = ("merged_from", "merge")


def render(**kw: Any) -> str:
    return json.dumps(ranked(**kw), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":  # pragma: no cover - fixture writer (structlog logs to stdout)
    from pathlib import Path

    Path(sys.argv[1]).write_text(render(with_counts=False))
