"""The ``levels`` routine (E23.0, PLAN D98): volume profile + OI walls per ticker.

Once per trading day, pre-market, for the active list + open underlyings (+ today's
candidates) and SPY/QQQ/IWM:

* **Volume profile:** one ``history_bars`` request for the last ``sessions`` completed
  sessions of ``bar`` bars on the history feed (``data.alpaca_history_feed``, SIP by
  default, E23.0a), regular-hours bars only, binned at
  ``max(tick, ATR14 x bin_atr)`` (ATR14 from the same sessions' RTH OHLC) by
  :func:`arc.features.volume_profile.build_profile`.
* **OI walls:** one ``option_chain`` call over the swing sleeve's expiry window
  (the active profile's sleeve named ``swing``, else its longest-dated sleeve), walls
  measured against the last profile close (no live quote: pre-market mids are not
  usable, see ``arc.data.base.reference_price``).

Every request first takes a slot from the shared Alpaca data budget
(``routine_state[alpaca_data:calls]``); an option chain costs :data:`CHAIN_CALLS`
slots (snapshots, contracts, daily volume). A ticker's failure is recorded and the
rest continue; no new ticker starts after ``max_runtime_s``. Context only: the
regime step merges the result into the day's technicals; never a gate input.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import structlog

from arc.features.levels import LevelsPayload, VolumeProfileLevels
from arc.features.oi_walls import oi_walls
from arc.features.technicals import wilder_atr
from arc.features.volume_profile import (
    bin_width_for,
    build_profile,
    rth_bars,
    session_ohlc,
)
from arc.utils.calendar import previous_session

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from arc.account_profiles import DteSleeve
    from arc.data.base import MarketDataProvider
    from arc.routines.config import OiWallsSettings, VolumeProfileSettings

log = structlog.get_logger(__name__)

#: D56 market reference: always refreshed.
REFERENCE = ("SPY", "QQQ", "IWM")
#: Budget slots one ``option_chain`` call takes (snapshots + contracts + daily volume).
CHAIN_CALLS = 3
SWING_SLEEVE = "swing"


@dataclass
class LevelsResult:
    day: _dt.date
    payloads: list[LevelsPayload] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    not_started: list[str] = field(default_factory=list)
    calls: int = 0
    elapsed_s: float = 0.0

    def metrics(self) -> dict[str, Any]:
        vp = [p for p in self.payloads if p.volume_profile is not None]
        walls = [p for p in self.payloads if p.oi_walls is not None]
        return {
            "tickers": len(self.payloads),
            "profiles": len(vp),
            "oi_walls": len(walls),
            "errors": len(self.errors),
            "not_started": len(self.not_started),
            "alpaca_calls": self.calls,
            "elapsed_s": round(self.elapsed_s, 1),
        }

    def summary(self) -> str:
        m = self.metrics()
        parts = [
            f"levels for {m['tickers']} tickers ({self.day})",
            f"{m['profiles']} volume profiles",
            f"{m['oi_walls']} OI walls",
            f"{m['alpaca_calls']} Alpaca calls",
            f"{m['elapsed_s']:.0f}s",
        ]
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        if self.not_started:
            parts.append(f"{len(self.not_started)} not started (runtime cap)")
        return " · ".join(parts)


def level_tickers(tickers: Sequence[str]) -> list[str]:
    """*tickers* upper-cased and deduplicated, then SPY/QQQ/IWM (input order kept)."""
    return list(dict.fromkeys([*(t.upper() for t in tickers), *REFERENCE]))


def profile_sessions(day: _dt.date, n: int) -> list[_dt.date]:
    """The *n* completed sessions strictly before *day*, ascending."""
    out: list[_dt.date] = []
    cur = day
    for _ in range(n):
        cur = previous_session(cur)
        out.append(cur)
    return sorted(out)


def swing_window(sleeves: Sequence[DteSleeve], day: _dt.date) -> tuple[_dt.date, _dt.date] | None:
    """Expiry window of the ``swing`` sleeve (else the longest-dated one); ``None``: none."""
    if not sleeves:
        return None
    s = next((x for x in sleeves if x.name == SWING_SLEEVE), None)
    s = s or max(sleeves, key=lambda x: (x.dte_max, x.dte_min))
    return day + _dt.timedelta(days=s.dte_min), day + _dt.timedelta(days=s.dte_max)


def _profile(
    bars: list[Any],
    sessions: list[_dt.date],
    vp_cfg: VolumeProfileSettings,
    feed: Literal["sip", "iex"] | None,
) -> tuple[VolumeProfileLevels | None, float | None, str | None]:
    """``(profile, last RTH close, why-missing)`` from intraday *bars*."""
    rth = rth_bars(bars, sessions)
    if not rth:
        return None, None, "volume_profile: no regular-hours bars"
    daily = session_ohlc(rth)
    last_close = daily[-1].close
    h = np.array([d.high for d in daily], dtype=float)
    lo = np.array([d.low for d in daily], dtype=float)
    c = np.array([d.close for d in daily], dtype=float)
    atr = wilder_atr(h, lo, c)
    width = bin_width_for(atr, vp_cfg.bin_atr)
    prof = build_profile(
        rth, bin_width=width, value_area=vp_cfg.value_area, hvn_min_share=vp_cfg.hvn_min_share
    )
    if prof is None:
        return None, last_close, "volume_profile: zero volume"
    return (
        VolumeProfileLevels(
            **prof.model_dump(),
            sessions=len(daily),
            first_session=daily[0].day,
            last_session=daily[-1].day,
            bar=vp_cfg.bar,
            feed=feed,
            bars=len(rth),
            atr14=None if atr is None else round(atr, 6),
        ),
        last_close,
        None,
    )


def refresh_levels(
    market: MarketDataProvider,
    tickers: Sequence[str],
    day: _dt.date,
    *,
    vp_cfg: VolumeProfileSettings,
    oi_cfg: OiWallsSettings,
    sleeves: Sequence[DteSleeve],
    feed: Literal["sip", "iex"] | None,
    take: Callable[[], object] | None = None,
    max_runtime_s: float = 900.0,
    clock: Callable[[], float] = time.monotonic,
) -> LevelsResult:
    """Levels for *tickers* (+ SPY/QQQ/IWM) for trading day *day*.

    Uses only sessions strictly before *day* (pre-market: *day*'s bars do not exist
    yet; a by-hand run mid-session must not use a partial session).
    """
    out = LevelsResult(day=day)
    started = clock()
    sessions = profile_sessions(day, vp_cfg.sessions)
    window = swing_window(sleeves, day) if oi_cfg.enabled else None

    def slot(n: int = 1) -> None:
        for _ in range(n):
            if take is not None:
                take()
            out.calls += 1

    names = level_tickers(tickers)
    for i, t in enumerate(names):
        if clock() - started >= max_runtime_s:
            out.not_started = names[i:]
            log.warning("levels.runtime_cap", not_started=len(out.not_started))
            break
        missing: list[str] = []
        prof = None
        spot: float | None = None
        try:
            # the closes of the profile window double as the walls' reference spot
            slot()
            bars = market.history_bars(t, sessions[0], sessions[-1], vp_cfg.bar, feed=feed)
            prof, spot, why = _profile(list(bars), sessions, vp_cfg, feed)
            if not vp_cfg.enabled:
                prof = None
            elif why:
                missing.append(why)
        except Exception as exc:  # noqa: BLE001 - one ticker must not sink the rest
            out.errors[t] = f"bars: {exc}"[:300]
            log.warning("levels.bars_failed", ticker=t, error=str(exc)[:200])
            continue
        walls = None
        if oi_cfg.enabled:
            if window is None:
                missing.append("oi_walls: no entry sleeve")
            elif spot is None:
                missing.append("oi_walls: no reference close")
            else:
                try:
                    slot(CHAIN_CALLS)
                    chain = market.option_chain(t, window[0], window[1])
                    expiries = {c.expiration for c in chain if window[0] <= c.expiration}
                    walls = oi_walls(chain, spot, expiries, min_oi=oi_cfg.min_oi)
                    if walls is not None and walls.contracts_with_oi == 0:
                        missing.append("oi_walls: no open interest")
                except Exception as exc:  # noqa: BLE001 - walls are optional context
                    missing.append(f"oi_walls: {exc}"[:200])
                    log.warning("levels.chain_failed", ticker=t, error=str(exc)[:200])
        out.payloads.append(
            LevelsPayload(ticker=t, as_of=day, volume_profile=prof, oi_walls=walls, missing=missing)
        )
    out.elapsed_s = clock() - started
    log.info("levels.refreshed", **out.metrics())
    return out
