"""Stance proxies for the robust study protocol (card E7.20, PLAN D125).

Live, an LLM (Research) sets each idea's stance; the backtest cannot replay it, so a
deterministic proxy stands in. One proxy is one assumption about how good (or bad) the
live stance is. A study's robustness grid (:class:`arc.backtest.study.StudyRobustness`)
walks the same menus under several proxies, so a treatment must win whatever the stance
quality turns out to be.

Every proxy maps (ticker, session) to a trend-label stance read from data ≤ that
session: ``bull`` / ``bear`` / ``sideways`` (the stance filter maps these to the
profile's ``stance_strategies``: bullish / bearish / neutral), ``unknown`` (no trade,
as for an unlabelled session) or :data:`ANY` (no stance filter: every direction the
profile trades competes).

* ``trend_v2``: the ticker's own trend label (``backtest.sim.regime_model: v2``,
  D77). The control proxy: the stance the sim has always used.
* ``noisy_<p>``: ``trend_v2`` with each (ticker, ISO week) flipped with probability
  *p*, so a flip persists for the week like an LLM view. Flip: bull ↔ bear, sideways →
  bull or bear (a second draw). The RNG is seeded by ``(seed key, ticker, ISO week)``;
  the study runner passes the spec hash, so the flips reproduce exactly and are the
  same for every arm of a study.
* ``lagged_<k>``: ``trend_v2`` from *k* sessions earlier (a slow analyst).
* ``market_v2``: the market reference's (SPY) ``trend_v2`` label for every name.
* ``both``: no stance filter (an upper bound on menu breadth).
* ``live_llm``: replay of the live Research stances (E7.22). Reserved: refused until
  that card lands.

Pure functions; no I/O, no clock.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - runtime use in signatures
import hashlib
from typing import Literal, get_args

import numpy as np
import pandas as pd

__all__ = [
    "ANY",
    "CONTROL_PROXY",
    "LABELS",
    "StanceProxy",
    "flip_weeks",
    "labels_needed",
    "proxy_labels",
]

#: The stance proxies a robustness grid may name (E7.20; ``live_llm`` is E7.22's slot).
StanceProxy = Literal[
    "trend_v2", "noisy_0.2", "noisy_0.35", "lagged_5", "market_v2", "both", "live_llm"
]
PROXIES: tuple[str, ...] = get_args(StanceProxy)
#: The proxy of the control cell (the sim's stance before E7.20).
CONTROL_PROXY = "trend_v2"
#: Stance value meaning "no stance filter" (the ``both`` proxy).
ANY = "*"
#: Every stance a proxy can give a session (``unknown`` = no trade, never menu-tagged).
LABELS: tuple[str, ...] = ("bull", "bear", "sideways", ANY)
_FLIP = {"bull": "bear", "bear": "bull"}


def _param(proxy: str) -> float:
    return float(proxy.rsplit("_", 1)[1])


def labels_needed(proxies: tuple[str, ...] | list[str]) -> frozenset[str]:
    """The stance values the build must tag candidates for, to walk *proxies*
    (empty = the untagged pre-E7.20 build: only ``trend_v2`` is walked)."""
    if set(proxies) <= {CONTROL_PROXY}:
        return frozenset()
    out: set[str] = set()
    if set(proxies) - {"both"}:
        out |= {"bull", "bear", "sideways"}
    if "both" in proxies:
        out.add(ANY)
    return frozenset(out)


def _week_draws(seed_key: str, ticker: str, week: tuple[int, int]) -> tuple[float, float]:
    blob = f"{seed_key}|{ticker.upper()}|{week[0]}-W{week[1]:02d}".encode()
    seed = int.from_bytes(hashlib.sha256(blob).digest()[:8], "big")
    u = np.random.default_rng(seed).random(2)
    return float(u[0]), float(u[1])


def flip_weeks(
    days: list[dt.date], *, ticker: str, p: float, seed_key: str
) -> dict[tuple[int, int], int]:
    """ISO week → 0 (no flip), 1 (flip to bull on sideways), 2 (flip to bear on
    sideways), for the weeks of *days*. Deterministic in (seed key, ticker, week)."""
    out: dict[tuple[int, int], int] = {}
    for d in days:
        iso = d.isocalendar()
        wk = (iso[0], iso[1])
        if wk in out:
            continue
        u, v = _week_draws(seed_key, ticker, wk)
        out[wk] = 0 if u >= p else (1 if v < 0.5 else 2)  # noqa: PLR2004
    return out


def proxy_labels(
    proxy: str,
    trend: pd.Series,
    *,
    ticker: str,
    market: pd.Series | None = None,
    seed_key: str = "",
) -> pd.Series:
    """Session → stance for one ticker under *proxy* (see the module docstring).

    *trend* is the ticker's own ``trend_v2`` label per session (closes ≤ that session),
    *market* the market reference's. The index of the result is *trend*'s.
    """
    if proxy not in PROXIES:
        msg = f"unknown stance proxy {proxy!r} (one of {', '.join(PROXIES)})"
        raise ValueError(msg)
    t = trend.sort_index().astype(str)
    if proxy == "trend_v2":
        return t
    if proxy == "both":
        return pd.Series(ANY, index=t.index, dtype=object)
    if proxy == "market_v2":
        if market is None:
            msg = "market_v2 needs the market reference's labels (no SPY closes loaded)"
            raise ValueError(msg)
        m = market.astype(str)
        return pd.Series([m.get(d, "unknown") for d in t.index], index=t.index, dtype=object)
    if proxy.startswith("lagged_"):
        k = int(_param(proxy))
        vals = list(t.to_numpy())
        lag = ["unknown"] * min(k, len(vals)) + vals[: max(len(vals) - k, 0)]
        return pd.Series(lag, index=t.index, dtype=object)
    if proxy.startswith("noisy_"):
        p = _param(proxy)
        weeks = flip_weeks(list(t.index), ticker=ticker, p=p, seed_key=seed_key)
        out = []
        for d, lab in t.items():
            iso = d.isocalendar()
            f = weeks[(iso[0], iso[1])]
            if f == 0 or lab not in ("bull", "bear", "sideways"):
                out.append(lab)
            elif lab == "sideways":
                out.append("bull" if f == 1 else "bear")
            else:
                out.append(_FLIP[lab])
        return pd.Series(out, index=t.index, dtype=object)
    msg = f"stance proxy {proxy!r} is not available yet (E7.22 replays the live LLM stance)"
    raise ValueError(msg)
