"""E2.5 (D116): scanner liquidity inputs.

Open interest from Cboe's delayed chain (Alpaca as fallback, stale Alpaca → unknown)
and option volume from today's daily bar only, with the early-session pro-rated
volume minimum. Offline: Cboe bodies are built in the test, the clock is injected.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

from arc.data.base import OptionContract, OptionGreeks
from arc.data.oi import (
    CboeOpenInterestMarket,
    alpaca_oi_cutoff,
    apply_open_interest,
    merge_open_interest,
    modal_date,
    stale_alpaca_oi,
)
from arc.data.volume import (
    VolumeBasis,
    bar_date,
    pick_volume,
    today_min_volume,
    volume_basis,
)
from arc.scanner.filters import LiquidityRules, Reject, check_contract
from arc.utils.calendar import ET

WED = dt.date(2026, 10, 14)  # a session; previous session Tue Oct 13
TUE = dt.date(2026, 10, 13)
MON = dt.date(2026, 10, 12)
EARLY = dt.date(2026, 11, 27)  # day after Thanksgiving: 13:00 close


def et(d: dt.date, h: int, m: int = 0) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def _bar(v: int, day: dt.date) -> dict[str, Any]:
    return {"v": v, "t": f"{day.isoformat()}T04:00:00Z", "c": 1.0}


# ---------------------------------------------------------------------------
# Open interest merge
# ---------------------------------------------------------------------------

A = "MU261113C00950000"
B = "MU261127C00930000"  # listed after Alpaca's OI date: no Alpaca OI
C = "MU261113P00800000"


class TestMerge:
    def test_cboe_present_wins(self) -> None:
        out = merge_open_interest({A: (14, TUE)}, {A: 298}, cutoff=TUE)
        assert out == {A: (298, "cboe")}

    def test_cboe_covers_contract_alpaca_lacks(self) -> None:
        out = merge_open_interest({B: (None, None)}, {B: 184}, cutoff=TUE)
        assert out == {B: (184, "cboe")}

    def test_cboe_missing_fresh_alpaca_used(self) -> None:
        out = merge_open_interest({A: (14, TUE)}, {C: 5}, cutoff=TUE)
        assert out == {A: (14, "alpaca")}

    def test_cboe_symbol_with_null_oi_falls_back(self) -> None:
        out = merge_open_interest({A: (14, TUE)}, {A: None}, cutoff=TUE)
        assert out == {A: (14, "alpaca")}

    def test_stale_alpaca_is_unknown(self) -> None:
        out = merge_open_interest({A: (14, MON)}, {}, cutoff=TUE)
        assert out == {A: (None, None)}

    def test_undated_alpaca_is_unknown(self) -> None:
        assert merge_open_interest({A: (14, None)}, {}, cutoff=TUE) == {A: (None, None)}

    def test_cboe_fetch_error_falls_back_to_fresh_alpaca(self) -> None:
        out = merge_open_interest({A: (14, TUE), B: (None, None)}, None, cutoff=TUE)
        assert out == {A: (14, "alpaca"), B: (None, None)}

    def test_padded_symbols_match(self) -> None:
        out = merge_open_interest({"MU    261113C00950000": (14, TUE)}, {A: 298}, cutoff=TUE)
        assert out == {"MU    261113C00950000": (298, "cboe")}

    @pytest.mark.parametrize(
        ("as_of", "cutoff"),
        [
            (WED, TUE),
            (dt.date(2026, 10, 17), dt.date(2026, 10, 15)),  # Saturday: last session Fri
            (dt.date(2026, 10, 19), dt.date(2026, 10, 16)),  # Monday: Friday
        ],
    )
    def test_cutoff_is_previous_session(self, as_of: dt.date, cutoff: dt.date) -> None:
        assert alpaca_oi_cutoff(as_of) == cutoff

    @given(
        st.dictionaries(
            st.sampled_from([A, B, C]),
            st.tuples(
                st.one_of(st.none(), st.integers(0, 10**6)),
                st.one_of(st.none(), st.sampled_from([MON, TUE, WED])),
            ),
        ),
        st.one_of(
            st.none(),
            st.dictionaries(
                st.sampled_from([A, B, C]), st.one_of(st.none(), st.integers(0, 10**6))
            ),
        ),
    )
    def test_merge_properties(
        self,
        alpaca: dict[str, tuple[int | None, dt.date | None]],
        cboe: dict[str, int | None] | None,
    ) -> None:
        out = merge_open_interest(alpaca, cboe, cutoff=TUE)
        assert set(out) == set(alpaca)
        for sym, (oi, src) in out.items():
            if cboe is not None and cboe.get(sym) is not None:
                assert (oi, src) == (cboe[sym], "cboe")
            elif src == "alpaca":
                a_oi, a_date = alpaca[sym]
                assert oi == a_oi and a_date is not None and a_date >= TUE
            else:
                assert (oi, src) == (None, None)


def _contract(sym: str, oi: int | None, oi_date: dt.date | None, vol: int = 50) -> OptionContract:
    return OptionContract(
        symbol=sym,
        underlying="MU",
        expiration=dt.date(2026, 11, 13),
        strike=950.0,
        option_type="call",
        bid=1.0,
        ask=1.05,
        mid=1.025,
        open_interest=oi,
        open_interest_date=oi_date,
        volume=vol,
        volume_source="today",
        implied_volatility=0.5,
        greeks=OptionGreeks(delta=0.3),
    )


class TestApply:
    def test_counts_and_sources(self) -> None:
        chain = [_contract(A, 14, TUE), _contract(B, None, None), _contract(C, 7, MON)]
        out, counts = apply_open_interest(chain, {A: 298, B: 184}, as_of=WED)
        assert [(c.open_interest, c.oi_source) for c in out] == [
            (298, "cboe"),
            (184, "cboe"),
            (None, None),
        ]
        assert (counts.oi_cboe, counts.oi_alpaca, counts.oi_unknown) == (2, 0, 1)
        assert counts.cboe_ok

    def test_unset_sources_left_out_of_dumps(self) -> None:
        """Recordings / tapes / hashes of contracts without the new fields are unchanged."""
        bare = OptionContract(
            symbol=A, underlying="MU", expiration=WED, strike=1.0, option_type="call"
        )
        dumped = bare.model_dump(mode="json")
        assert not {"open_interest_date", "oi_source", "volume_source"} & set(dumped)
        assert OptionContract.model_validate(dumped) == bare
        tagged = _contract(A, 14, TUE).model_copy(update={"oi_source": "cboe"})
        assert tagged.model_dump(mode="json")["oi_source"] == "cboe"
        assert OptionContract.model_validate(tagged.model_dump(mode="json")) == tagged


def _cboe_body(rows: dict[str, int | None]) -> bytes:
    return json.dumps(
        {
            "timestamp": "2026-10-14 15:00:00",
            "data": {
                "symbol": "MU",
                "current_price": 900.0,
                "options": [
                    {"option": s, "bid": 1.0, "ask": 1.1, "open_interest": oi, "volume": 3}
                    for s, oi in rows.items()
                ],
            },
        }
    ).encode()


class _Inner:
    def __init__(self, chain: list[OptionContract]) -> None:
        self.chain = chain
        self.calls = 0
        self.extra = "passthrough"

    def option_chain(self, underlying: str, s: dt.date, e: dt.date) -> list[OptionContract]:
        self.calls += 1
        return list(self.chain)

    def underlying_quote(self, symbol: str) -> str:
        return f"quote:{symbol}"

    def history_bars(self, *args: Any, **kw: Any) -> tuple[Any, Any]:
        return args, kw


class TestCboeMarket:
    def _market(
        self, get: Any, chain: list[OptionContract] | None = None
    ) -> tuple[CboeOpenInterestMarket, _Inner]:
        inner = _Inner(chain or [_contract(A, 14, TUE), _contract(B, None, None)])
        return CboeOpenInterestMarket(inner, get=get, today=lambda: WED), inner  # type: ignore[arg-type]

    def test_cboe_primary_and_one_fetch_per_ticker(self) -> None:
        urls: list[str] = []

        def get(url: str) -> bytes:
            urls.append(url)
            return _cboe_body({A: 298, B: 184})

        mkt, _ = self._market(get)
        first = mkt.option_chain("MU", WED, WED)
        mkt.option_chain("mu", WED, WED)
        assert [(c.open_interest, c.oi_source) for c in first] == [(298, "cboe"), (184, "cboe")]
        assert urls == ["https://cdn.cboe.com/api/global/delayed_quotes/options/MU.json"]

    def test_fetch_error_falls_back_to_alpaca_and_is_cached(self) -> None:
        n = 0

        def get(url: str) -> bytes:
            nonlocal n
            n += 1
            raise ConnectionError("cdn down")

        mkt, _ = self._market(get)
        with capture_logs() as logs:
            out = mkt.option_chain("MU", WED, WED)
            mkt.option_chain("MU", WED, WED)
        assert [(c.open_interest, c.oi_source) for c in out] == [(14, "alpaca"), (None, None)]
        assert n == 1
        (ev,) = [e for e in logs if e["event"] == "scanner.oi_source"]
        assert ev["oi_source"] == "alpaca_fallback" and ev["ticker"] == "MU"

    @pytest.mark.parametrize("body", [b"<html>blocked</html>", _cboe_body({})])
    def test_bad_body_falls_back(self, body: bytes) -> None:
        mkt, _ = self._market(lambda url: body)
        out = mkt.option_chain("MU", WED, WED)
        assert out[0].oi_source == "alpaca"

    def test_passthrough_calls(self) -> None:
        mkt, inner = self._market(lambda url: _cboe_body({}))
        assert mkt.underlying_quote("MU") == "quote:MU"  # type: ignore[comparison-overlap]
        assert mkt.history_bars("MU", WED, WED) == (("MU", WED, WED, "1Day"), {})  # type: ignore[comparison-overlap]
        assert mkt.history_bars("MU", WED, WED, "1Min", feed="sip") == (  # type: ignore[comparison-overlap]
            ("MU", WED, WED, "1Min"),
            {"feed": "sip"},
        )
        assert mkt.extra == "passthrough"

    def test_stale_alpaca_dates_tallied(self) -> None:
        chain = [_contract(A, 14, MON), _contract(C, 3, MON), _contract(B, None, None)]
        mkt, _ = self._market(lambda url: _cboe_body({A: 1}), chain)
        mkt.option_chain("MU", WED, WED)
        assert stale_alpaca_oi(mkt.alpaca_dates, WED) == MON
        assert stale_alpaca_oi(Counter({TUE: 5, MON: 1}), WED) is None
        assert stale_alpaca_oi(Counter({None: 3}), WED) is None

    def test_modal_date_tie_takes_latest(self) -> None:
        assert modal_date(Counter({MON: 2, TUE: 2, None: 9})) == TUE
        assert modal_date(Counter()) is None


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


class TestVolumeBasis:
    def test_pre_ten_reads_previous_session_full_minimum(self) -> None:
        b = volume_basis(et(WED, 9, 59))
        assert b == VolumeBasis(source="prev_session", session=TUE, fraction=1.0)

    def test_premarket_reads_previous_session(self) -> None:
        assert volume_basis(et(WED, 7, 0)).source == "prev_session"

    def test_non_session_day_reads_last_session(self) -> None:
        b = volume_basis(et(dt.date(2026, 10, 17), 12, 0))
        assert (b.source, b.session) == ("prev_session", dt.date(2026, 10, 16))

    @pytest.mark.parametrize(
        ("hm", "fraction", "minimum"),
        [
            ((10, 0), 30 / 195, 2),  # 30 of 195 min to midday → ceil(10 × 0.154) = 2
            ((11, 15), 105 / 195, 6),
            ((12, 45), 1.0, 10),  # 09:30 + 3h15 = midday
            ((15, 0), 1.0, 10),
            ((17, 0), 1.0, 10),  # after the close: full day
        ],
    )
    def test_mid_session_pro_rating(
        self, hm: tuple[int, int], fraction: float, minimum: int
    ) -> None:
        b = volume_basis(et(WED, *hm))
        assert b.source == "today" and b.session == WED
        assert b.fraction == pytest.approx(fraction)
        assert today_min_volume(10, b.fraction) == minimum

    def test_early_close_reaches_full_threshold_sooner(self) -> None:
        # 09:30-13:00 → midday 11:15; at 10:30, 60 of 105 minutes
        b = volume_basis(et(EARLY, 10, 30))
        assert b.fraction == pytest.approx(60 / 105)
        assert volume_basis(et(EARLY, 11, 15)).fraction == 1.0

    def test_naive_now_is_et(self) -> None:
        assert volume_basis(dt.datetime(2026, 10, 14, 12, 45)).fraction == 1.0

    @pytest.mark.parametrize(
        ("minimum", "fraction", "want"),
        [(10, 0.0, 1), (10, 0.01, 1), (10, 0.5, 5), (10, 1.0, 10), (0, 0.5, 0), (3, 2.0, 3)],
    )
    def test_today_min_volume(self, minimum: int, fraction: float, want: int) -> None:
        assert today_min_volume(minimum, fraction) == want

    @given(st.integers(1, 10_000), st.floats(0.0, 1.0))
    def test_today_min_volume_bounds(self, minimum: int, fraction: float) -> None:
        got = today_min_volume(minimum, fraction)
        assert 1 <= got <= minimum
        assert got >= minimum * fraction - 1e-6


class TestPickVolume:
    TODAY = VolumeBasis(source="today", session=WED, fraction=0.5)
    PREV = VolumeBasis(source="prev_session", session=TUE, fraction=1.0)

    def test_todays_bar(self) -> None:
        snap = {"dailyBar": _bar(96, WED), "prevDailyBar": _bar(10, TUE)}
        assert pick_volume(snap, self.TODAY) == 96

    def test_no_daily_bar_is_zero(self) -> None:
        assert pick_volume({"prevDailyBar": _bar(10, TUE)}, self.TODAY) == 0
        assert pick_volume({"dailyBar": None, "prevDailyBar": None}, self.TODAY) == 0
        assert pick_volume({}, self.TODAY) == 0

    def test_daily_bar_dated_yesterday_is_zero(self) -> None:
        """No print today yet: Alpaca's dailyBar is still yesterday's; it never counts."""
        snap = {"dailyBar": _bar(40, TUE), "prevDailyBar": _bar(10, MON)}
        assert pick_volume(snap, self.TODAY) == 0

    def test_pre_ten_previous_session_from_either_bar(self) -> None:
        before_print = {"dailyBar": _bar(40, TUE), "prevDailyBar": _bar(10, MON)}
        after_print = {"dailyBar": _bar(3, WED), "prevDailyBar": _bar(40, TUE)}
        assert pick_volume(before_print, self.PREV) == 40
        assert pick_volume(after_print, self.PREV) == 40
        assert pick_volume({"dailyBar": _bar(3, WED)}, self.PREV) == 0

    def test_bar_without_volume_or_date(self) -> None:
        assert pick_volume({"dailyBar": {"t": "2026-10-14T04:00:00Z"}}, self.TODAY) == 0
        assert pick_volume({"dailyBar": {"v": 5}}, self.TODAY) == 0

    @pytest.mark.parametrize(
        ("bar", "want"),
        [
            ({"t": "2026-10-14T04:00:00Z"}, WED),
            ({"t": "2026-10-14T00:00:00"}, WED),
            ({"t": "bad"}, None),
            ({"t": ""}, None),
            ({"t": 5}, None),
            ({}, None),
        ],
    )
    def test_bar_date(self, bar: dict[str, Any], want: dt.date | None) -> None:
        assert bar_date(bar) == want


class TestFilterThreshold:
    def _c(self, vol: int, source: str | None) -> OptionContract:
        return _contract(A, 500, TUE, vol).model_copy(update={"volume_source": source})

    def test_today_volume_faces_pro_rated_minimum(self) -> None:
        rules = LiquidityRules(min_volume=10, volume_fraction=0.3)  # → 3
        assert check_contract(self._c(3, "today"), rules) is None
        assert check_contract(self._c(2, "today"), rules) is Reject.LOW_VOLUME

    def test_prev_session_and_unlabelled_face_full_minimum(self) -> None:
        rules = LiquidityRules(min_volume=10, volume_fraction=0.3)
        assert check_contract(self._c(9, "prev_session"), rules) is Reject.LOW_VOLUME
        assert check_contract(self._c(9, None), rules) is Reject.LOW_VOLUME
        assert check_contract(self._c(10, None), rules) is None

    def test_zero_volume_today_always_fails(self) -> None:
        rules = LiquidityRules(min_volume=10, volume_fraction=0.0)
        assert check_contract(self._c(0, "today"), rules) is Reject.LOW_VOLUME


# ---------------------------------------------------------------------------
# Ops alert (pipeline)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    from arc.store.db import connect
    from arc.store.migrate import migrate

    c = connect(":memory:")
    migrate(c)
    return c


class TestStaleOiAlert:
    def _env(self, dates: Counter[dt.date | None]) -> SimpleNamespace:
        mkt = CboeOpenInterestMarket(_Inner([]), get=lambda u: b"", today=lambda: WED)  # type: ignore[arg-type]
        mkt.alpaca_dates = dates
        # wrapped like the experiment tape recorder (``.inner``)
        return SimpleNamespace(market=SimpleNamespace(inner=mkt))

    def test_one_alert_per_day(self, conn: sqlite3.Connection) -> None:
        from arc.pipeline.steps import _alpaca_oi_alert

        ctx = SimpleNamespace(conn=conn, now=et(WED, 11, 0))
        env = self._env(Counter({MON: 100, None: 20}))
        _alpaca_oi_alert(ctx, env, WED)  # type: ignore[arg-type]
        _alpaca_oi_alert(ctx, env, WED)  # type: ignore[arg-type]
        rows = conn.execute("SELECT key, kind, resolved_at, correlation FROM ops_alerts").fetchall()
        assert len(rows) == 1
        key, kind, resolved, corr = rows[0]
        assert (key, kind) == ("data:alpaca_oi_stale:2026-10-14", "data:alpaca_oi_stale")
        assert resolved is not None  # informational one-off
        assert json.loads(corr)["modal_oi_date"] == "2026-10-12"

    def test_fresh_alpaca_no_alert(self, conn: sqlite3.Connection) -> None:
        from arc.pipeline.steps import _alpaca_oi_alert

        ctx = SimpleNamespace(conn=conn, now=et(WED, 11, 0))
        _alpaca_oi_alert(ctx, self._env(Counter({TUE: 100})), WED)  # type: ignore[arg-type]
        assert conn.execute("SELECT count(*) FROM ops_alerts").fetchone()[0] == 0

    def test_unwrapped_market_no_alert(self, conn: sqlite3.Connection) -> None:
        from arc.pipeline.steps import _alpaca_oi_alert

        ctx = SimpleNamespace(conn=conn, now=et(WED, 11, 0))
        _alpaca_oi_alert(ctx, SimpleNamespace(market=_Inner([])), WED)  # type: ignore[arg-type]
        assert conn.execute("SELECT count(*) FROM ops_alerts").fetchone()[0] == 0

    def test_db_error_never_raises(self) -> None:
        from arc.pipeline.steps import _alpaca_oi_alert

        broken = sqlite3.connect(":memory:")  # no ops_alerts table
        ctx = SimpleNamespace(conn=broken, now=et(WED, 11, 0))
        _alpaca_oi_alert(ctx, self._env(Counter({MON: 1})), WED)  # type: ignore[arg-type]
