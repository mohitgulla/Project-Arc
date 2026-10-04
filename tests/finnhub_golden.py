"""E4.8a golden helper: a fixed context snapshot and the prompts built from it.

Imported by tests/test_finnhub_persona_context.py and run stand-alone against an
origin/main checkout (``python golden.py <repo>``) to pin the flag-off prompt hashes.
Uses only APIs that exist on main before E4.8a.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sys
from typing import Any


def _now() -> dt.datetime:
    from arc.utils.calendar import ET

    return dt.datetime(2026, 10, 6, 10, 15, tzinfo=ET)  # Tuesday, in session


def _entry(i: int, kind: str, subject: str, payload: dict[str, Any], age_h: float) -> Any:
    from arc.context.store import ContextEntry

    t = _now() - dt.timedelta(hours=age_h)
    return ContextEntry(
        id=f"ctx-{i:03d}",
        kind=kind,
        subject=subject,
        payload=payload,
        schema_version=1,
        produced_by="test",
        created_at=t,
        valid_from=t,
    )


def _cand(t: str, conf: float) -> dict[str, Any]:
    return {
        "ticker": t,
        "stance": "bullish",
        "catalyst_type": "earnings",
        "catalyst_date": "2026-10-28",
        "confidence": conf,
        "sources": [f"https://example.com/{t.lower()}"],
        "created_at": "2026-10-06T09:00:00-04:00",
    }


def finnhub_payloads(t: str, as_of: str = "2026-10-05") -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "earnings_history",
            {
                "ticker": t,
                "quarters": [
                    {
                        "period": "2026-06-30",
                        "actual": 1.91,
                        "estimate": 1.93,
                        "surprise": -0.02,
                        "surprise_pct": -0.89,
                    },
                    {
                        "period": "2026-03-31",
                        "actual": 2.01,
                        "estimate": 1.99,
                        "surprise": 0.02,
                        "surprise_pct": 1.09,
                    },
                    {
                        "period": "2025-12-31",
                        "actual": 2.84,
                        "estimate": 2.73,
                        "surprise": 0.11,
                        "surprise_pct": 4.19,
                    },
                    {
                        "period": "2025-09-30",
                        "actual": 1.85,
                        "estimate": 1.77,
                        "surprise": 0.08,
                        "surprise_pct": 4.52,
                    },
                    {
                        "period": "2025-06-30",
                        "actual": 1.57,
                        "estimate": 1.43,
                        "surprise": 0.14,
                        "surprise_pct": 9.79,
                    },
                ],
                "beat_count": 4,
                "miss_count": 1,
                "as_of": as_of,
                "source": "finnhub",
            },
        ),
        (
            "insider_activity",
            {
                "ticker": t,
                "window_days": 90,
                "buy_count": 3,
                "sell_count": 1,
                "net_shares": 12000,
                "net_value_usd": 2_350_000.0,
                "distinct_insiders_buying": 3,
                "distinct_insiders_selling": 1,
                "last_txn_date": "2026-09-20",
                "cluster_buy": True,
                "as_of": as_of,
                "source": "finnhub",
            },
        ),
        (
            "analyst_recs",
            {
                "ticker": t,
                "period": "2026-09-01",
                "strong_buy": 12,
                "buy": 22,
                "hold": 15,
                "sell": 3,
                "strong_sell": 1,
                "prev_period": {
                    "period": "2026-08-01",
                    "strong_buy": 13,
                    "buy": 24,
                    "hold": 14,
                    "sell": 3,
                    "strong_sell": 0,
                },
                "net_change": -4,
                "as_of": as_of,
                "source": "finnhub",
            },
        ),
        (
            "fundamentals",
            {
                "ticker": t,
                "beta": 1.21,
                "high_52w": 260.1,
                "high_52w_date": "2026-07-15",
                "low_52w": 169.2,
                "low_52w_date": "2026-04-08",
                "market_cap_musd": 3_400_000.0,
                "rel_sp500_4w": 2.4,
                "rel_sp500_13w": -3.1,
                "rel_sp500_26w": 5.0,
                "rel_sp500_52w": 1.0,
                "return_5d_pct": 1.2,
                "return_ytd_pct": 8.0,
                "forward_pe": 31.4,
                "eps_growth_ttm_yoy": 9.1,
                "revenue_growth_ttm_yoy": 5.2,
                "as_of": as_of,
                "source": "finnhub",
            },
        ),
    ]


def snapshot(*, with_finnhub: bool = True) -> Any:
    from arc.context.store import ContextSnapshot

    entries = [
        _entry(1, "candidate", "AAPL", _cand("AAPL", 0.8), 1),
        _entry(2, "candidate", "NVDA", _cand("NVDA", 0.7), 1),
        _entry(
            3,
            "regime",
            "AAPL",
            {"ticker": "AAPL", "as_of": "2026-10-05", "last_close": 247.5, "vol": {}},
            2,
        ),
    ]
    if with_finnhub:
        i = 10
        for t in ("AAPL", "NVDA"):
            for kind, p in finnhub_payloads(t):
                entries.append(_entry(i, kind, t, p, 20))
                i += 1
    return ContextSnapshot(id="snap-golden", as_of=_now(), entries=entries)


def director_prompt(snap: Any, **extra: Any) -> str:
    from arc.personas.builders import build_director_prompt, director_input_from_context

    inp = director_input_from_context(
        snap, portfolio_summary="flat", scan_date="2026-10-06", **extra
    )
    return build_director_prompt(inp)


def scout_prompt(**extra: Any) -> str:
    from arc.personas.builders import ScoutInput, build_scout_prompt

    return build_scout_prompt(
        ScoutInput(
            universe=["SPY", "AAPL"],
            raw_feeds=["[story s1] category=company tickers=AAPL,NVDA\n  summary"],
            scan_date="2026-10-06",
            min_confidence=0.6,
            output_schema_json="{}",
            open_universe=True,
            digests=True,
            **extra,
        )
    )


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


if __name__ == "__main__":  # pragma: no cover - run by hand against origin/main
    sys.path.insert(0, sys.argv[1])
    print("director", sha(director_prompt(snapshot())))
    print("scout", sha(scout_prompt()))
