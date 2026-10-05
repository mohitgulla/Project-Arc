"""E5.7 open universe (D28): symbol master, extraction, liquidity screen, guard, ingest."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.data.recorded import MULTI_NAME_FIXTURES, RecordedMarketData
from arc.pipeline import FIXTURE_NOW
from arc.pipeline.env import fixture_universe_guard
from arc.universe import (
    LiquidityMetrics,
    LiquidityThresholds,
    SymbolMaster,
    UniverseGuard,
    extract_tickers,
    load_symbol_master,
    load_universe_config,
    measure_liquidity,
    refresh_symbol_master,
    screen_liquidity,
)
from arc.universe.config import ExtractionConfig, SymbolMasterConfig
from arc.universe.guard import (
    REJECT_ILLIQUID,
    REJECT_NEW_TICKER_CAP,
    REJECT_NOT_IN_UNIVERSE,
    REJECT_UNKNOWN_SYMBOL,
)
from arc.universe.ingest import IngestUniverse
from arc.universe.master import build_symbol_master, normalize_symbol
from arc.utils.calendar import ET

NOW = dt.datetime(2026, 9, 28, 9, 0, tzinfo=ET)
FIXTURE_MASTER = Path("arc/pipeline/fixtures/symbol_master.json")

SEC_ROWS = [
    {"cik": 1045810, "name": "NVIDIA CORP", "ticker": "NVDA", "exchange": "Nasdaq"},
    {"cik": 1321655, "name": "Palantir", "ticker": "PLTR", "exchange": "Nasdaq"},
    {"cik": 1067983, "name": "BERKSHIRE HATHAWAY", "ticker": "BRK-B", "exchange": "NYSE"},
    {"cik": 914156, "name": "UFP TECHNOLOGIES", "ticker": "UFPT", "exchange": "Nasdaq"},
    {"cik": 1, "name": "Pink sheet co", "ticker": "PINKX", "exchange": "OTC"},
    {"cik": 2, "name": "No options co", "ticker": "NOOP", "exchange": "NYSE"},
]
ALPACA_ROWS = [
    {"symbol": "NVDA", "exchange": "NASDAQ", "tradable": True, "attributes": ["has_options"]},
    {"symbol": "PLTR", "exchange": "NASDAQ", "tradable": True, "attributes": ["has_options"]},
    {"symbol": "BRK.B", "exchange": "NYSE", "tradable": True, "attributes": ["has_options"]},
    {"symbol": "UFPT", "exchange": "NASDAQ", "tradable": True, "attributes": ["has_options"]},
    {"symbol": "SPY", "exchange": "ARCA", "tradable": True, "attributes": ["has_options"]},
    {"symbol": "OTCQ", "exchange": "OTC", "tradable": True, "attributes": ["has_options"]},
]


def _cfg(tmp_path: Path) -> SymbolMasterConfig:
    return load_universe_config().symbol_master.model_copy(update={"cache": tmp_path / "sm.json"})


def _master(tmp_path: Path) -> SymbolMaster:
    return build_symbol_master(SEC_ROWS, ALPACA_ROWS, _cfg(tmp_path), now=NOW)


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, universe=["SPY", "NVDA", "XOM"], **kw)  # type: ignore[call-arg]


# -- symbol master -------------------------------------------------------------


class TestSymbolMaster:
    def test_normalize(self) -> None:
        assert normalize_symbol(" $brk-b ") == "BRK.B"
        assert normalize_symbol("BRK/B") == "BRK.B"

    def test_merge(self, tmp_path: Path) -> None:
        m = _master(tmp_path)
        assert "BRK.B" in m and "PINKX" not in m and "OTCQ" not in m
        assert m.sources == {"sec": 5, "alpaca": 5}
        assert m.symbols["SPY"].sources == ["alpaca"]  # ETF: not in the SEC company file
        assert m.symbols["NVDA"].sources == ["sec", "alpaca"]
        assert m.symbols["NVDA"].cik == 1045810
        assert m.symbols["NOOP"].options is False
        assert m.not_optionable("NOOP")
        assert m.not_optionable("NVDA") is None

    def test_alpaca_down_keeps_sec(self, tmp_path: Path) -> None:
        m = build_symbol_master(SEC_ROWS, None, _cfg(tmp_path), now=NOW)
        assert "NVDA" in m and m.symbols["NVDA"].options is None
        assert m.not_optionable("NVDA") is None  # unknown flags never reject

    def test_refresh_writes_cache_and_load_reads_it(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)

        def alpaca_down() -> list[dict[str, Any]]:
            raise RuntimeError("boom")

        m = refresh_symbol_master(
            cfg, user_agent="t", now=NOW, sec_fetcher=lambda: SEC_ROWS, alpaca_fetcher=alpaca_down
        )
        assert Path(cfg.cache).is_file()
        loaded = load_symbol_master(cfg, user_agent="t", now=NOW, fetch_if_missing=False)
        assert loaded is not None and set(loaded.symbols) == set(m.symbols)

    def test_stale_cache_is_still_used(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        Path(cfg.cache).write_text(_master(tmp_path).model_dump_json())
        later = NOW + dt.timedelta(days=cfg.refresh_days + 3)
        loaded = load_symbol_master(cfg, user_agent="t", now=later, fetch_if_missing=False)
        assert loaded is not None and loaded.is_stale(later, cfg.refresh_days)

    def test_missing_cache_never_fetches_when_told_not_to(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)

        def refresher() -> SymbolMaster:
            raise AssertionError("must not fetch")

        assert (
            load_symbol_master(
                cfg, user_agent="t", now=NOW, fetch_if_missing=False, refresher=refresher
            )
            is None
        )

    def test_corrupt_cache_fetch_failure_is_none(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)
        Path(cfg.cache).write_text("{not json")

        def refresher() -> SymbolMaster:
            raise RuntimeError("sec down")

        assert load_symbol_master(cfg, user_agent="t", now=NOW, refresher=refresher) is None


# -- extraction ------------------------------------------------------------------


class TestExtract:
    ACCEPTED = frozenset({"NVDA", "PLTR", "BRK.B", "AI", "IT", "F", "SPY"})
    CFG = load_universe_config().extraction

    def test_forms(self) -> None:
        text = (
            "Palantir (NASDAQ: PLTR) rallied; $nvda too. Berkshire (BRK-B) flat. "
            "SPY closed green. IT spending and AI hype; F is a letter."
        )
        assert extract_tickers(text, self.ACCEPTED, self.CFG) == ["PLTR", "NVDA", "BRK.B", "SPY"]

    def test_cashtag_beats_stop_word_and_length(self) -> None:
        assert extract_tickers("$AI and $F", self.ACCEPTED, self.CFG) == ["AI", "F"]

    def test_unknown_symbols_are_dropped(self) -> None:
        assert extract_tickers("$ZZZQ and (NYSE: QQQX) and CEO", self.ACCEPTED, self.CFG) == []

    def test_html_entities(self) -> None:
        assert extract_tickers("S&amp;P and &#36;NVDA", self.ACCEPTED, self.CFG) == ["NVDA"]

    def test_stop_words_are_strings(self) -> None:
        # YAML 1.1 would read bare ON / NO / YES as booleans.
        assert all(isinstance(w, str) for w in self.CFG.stop_words)
        assert {"ON", "NO", "YES"} <= set(self.CFG.stop_words)

    @given(st.text(max_size=200))
    @settings(max_examples=200, deadline=None)
    def test_only_accepted_symbols(self, text: str) -> None:
        out = extract_tickers(text, self.ACCEPTED, ExtractionConfig())
        assert set(out) <= self.ACCEPTED
        assert len(out) == len(set(out))


# -- screen ------------------------------------------------------------------------

T = LiquidityThresholds()
GOOD = LiquidityMetrics(
    ticker="X",
    as_of=NOW.date(),
    price=50.0,
    adv_shares=5e6,
    adv_sessions=20,
    expiries_in_window=3,
    atm_open_interest=2_000,
    atm_spread_pct=0.03,
)


class TestScreen:
    def test_good_passes(self) -> None:
        assert screen_liquidity(GOOD, T).passed

    @pytest.mark.parametrize(
        ("field", "value", "needle"),
        [
            ("price", 4.0, "price"),
            ("adv_shares", 10.0, "ADV"),
            ("expiries_in_window", 0, "DTE window"),
            ("atm_open_interest", 10, "near-ATM OI"),
            ("atm_spread_pct", 0.5, "ATM spread"),
            ("price", None, "price"),
            ("adv_shares", None, "ADV"),
            ("atm_open_interest", None, "near-ATM OI unknown"),
            ("atm_spread_pct", None, "ATM spread unknown"),
            ("error", "APIError: nope", "no market data"),
        ],
    )
    def test_each_check_fails_closed(self, field: str, value: object, needle: str) -> None:
        res = screen_liquidity(GOOD.model_copy(update={field: value}), T)
        assert not res.passed
        assert needle in res.detail()

    @given(
        st.floats(0, 500),
        st.floats(0, 1e8),
        st.integers(0, 10),
        st.integers(0, 50_000),
        st.floats(0, 2),
        st.floats(0, 100),
        st.floats(0, 1e7),
        st.integers(0, 5_000),
        st.floats(0, 0.5),
    )
    @settings(max_examples=300, deadline=None)
    def test_monotone(
        self,
        price: float,
        adv: float,
        exps: int,
        oi: int,
        spread: float,
        d_price: float,
        d_adv: float,
        d_oi: int,
        d_spread: float,
    ) -> None:
        """Better numbers never turn a pass into a fail."""
        m = LiquidityMetrics(
            ticker="X",
            as_of=NOW.date(),
            price=price,
            adv_shares=adv,
            adv_sessions=20,
            expiries_in_window=exps,
            atm_open_interest=oi,
            atm_spread_pct=spread,
        )
        better = m.model_copy(
            update={
                "price": price + d_price,
                "adv_shares": adv + d_adv,
                "expiries_in_window": exps + 1,
                "atm_open_interest": oi + d_oi,
                "atm_spread_pct": max(0.0, spread - d_spread),
            }
        )
        if screen_liquidity(m, T).passed:
            assert screen_liquidity(better, T).passed

    def test_measure_on_recordings(self) -> None:
        md = RecordedMarketData.from_files(*MULTI_NAME_FIXTURES)
        today = FIXTURE_NOW.date()
        pltr = measure_liquidity(md, "PLTR", today=today, dte_window=(21, 60), thresholds=T)
        assert pltr.error is None and pltr.adv_sessions >= T.adv_days
        assert screen_liquidity(pltr, T).passed
        ufpt = screen_liquidity(
            measure_liquidity(md, "UFPT", today=today, dte_window=(21, 60), thresholds=T), T
        )
        assert not ufpt.passed and "ADV" in ufpt.detail()

    def test_measure_never_raises(self) -> None:
        md = RecordedMarketData.from_files(*MULTI_NAME_FIXTURES)
        m = measure_liquidity(md, "NOPE", today=NOW.date(), dte_window=(21, 60), thresholds=T)
        assert m.error and not screen_liquidity(m, T).passed


# -- guard ---------------------------------------------------------------------------


def _fixture_guard(**kw: Any) -> UniverseGuard:
    md = RecordedMarketData.from_files(*MULTI_NAME_FIXTURES)
    return fixture_universe_guard(_settings(**kw), FIXTURE_NOW, md)


class TestGuard:
    def test_seed_mode(self) -> None:
        g = _fixture_guard()
        assert g.admit("NVDA") is None  # seed, never screened
        assert "NVDA" not in g.screens
        assert g.admit("PLTR") is None
        assert g.admitted_new == ["PLTR"]
        assert g.admit("pltr") is None  # counted once
        assert g.admit("UFPT") == REJECT_ILLIQUID
        assert "ADV" in g.details["UFPT"]
        assert g.admit("ZZZQ") == REJECT_UNKNOWN_SYMBOL

    def test_strict_mode(self) -> None:
        g = _fixture_guard(universe_mode="strict")
        assert g.admit("NVDA") is None
        assert g.admit("PLTR") == REJECT_NOT_IN_UNIVERSE
        assert not g.screens  # strict never measures

    def test_new_ticker_cap(self) -> None:
        g = _fixture_guard(scout_max_new_tickers=0)
        assert g.admit("PLTR") == REJECT_NEW_TICKER_CAP
        assert g.admit("NVDA") is None  # seed names never count

    def test_no_master_fails_closed(self, tmp_path: Path) -> None:
        g = UniverseGuard.from_settings(_settings(), now=NOW)  # hermetic: no cache
        assert g.master is None
        assert g.admit("PLTR") == REJECT_UNKNOWN_SYMBOL
        assert "unavailable" in g.details["PLTR"]
        assert g.admit("SPY") is None

    def test_no_market_data_fails_closed(self) -> None:
        master = SymbolMaster.model_validate_json(FIXTURE_MASTER.read_text())

        def broken() -> RecordedMarketData:
            raise RuntimeError("no keys")

        g = UniverseGuard.from_settings(
            _settings(), now=FIXTURE_NOW, master=master, market_factory=broken
        )
        assert g.admit("PLTR") == REJECT_ILLIQUID
        assert "no market data" in g.details["PLTR"]

    def test_not_optionable_rejects_before_screen(self) -> None:
        master = SymbolMaster.model_validate_json(FIXTURE_MASTER.read_text())
        info = master.symbols["PLTR"].model_copy(update={"options": False})
        master = master.model_copy(update={"symbols": {**master.symbols, "PLTR": info}})
        md = RecordedMarketData.from_files(*MULTI_NAME_FIXTURES)
        g = UniverseGuard.from_settings(
            _settings(), now=FIXTURE_NOW, master=master, market_factory=lambda: md
        )
        assert g.admit("PLTR") == REJECT_ILLIQUID
        assert not g.screens

    def test_gate_never_imports_universe(self) -> None:
        for path in Path("arc/gate").rglob("*.py"):
            assert "arc.universe" not in path.read_text(), path


# -- ingest -------------------------------------------------------------------------


class TestIngestUniverse:
    def _uni(self, **kw: Any) -> IngestUniverse:
        master = SymbolMaster.model_validate_json(FIXTURE_MASTER.read_text())
        return IngestUniverse.from_settings(_settings(**kw), master=master)

    def test_seed_mode_adds_master_symbols(self) -> None:
        uni = self._uni()
        text = "NVDA beat; Palantir (PLTR) and $UFPT moved. ZZZQ is noise."
        assert uni.tickers_in(text) == ["NVDA", "PLTR", "UFPT"]
        assert uni.known("PLTR") and not uni.known("ZZZQ")
        assert uni.mention_universe(text)[:3] == ["SPY", "NVDA", "XOM"]
        assert "PLTR" in uni.mention_universe(text)
        assert uni.cik("NVDA") == "0001045810"
        assert uni.cik("SPY") is None

    def test_strict_mode_is_seed_only(self) -> None:
        uni = self._uni(universe_mode="strict")
        assert uni.tickers_in("NVDA beat; Palantir (PLTR) moved") == ["NVDA"]
        assert not uni.known("PLTR")

    def test_no_master_is_seed_only(self) -> None:
        uni = IngestUniverse.from_settings(_settings())  # hermetic: no cache
        assert not uni.open
        assert uni.tickers_in("$PLTR and SPY") == ["SPY"]


# -- config / wiring ------------------------------------------------------------------


def test_universe_config_file_override(tmp_path: Path) -> None:
    path = tmp_path / "u.yaml"
    raw = Path("config/universe.yaml").read_text()
    path.write_text(raw.replace("min_atm_open_interest: 500", "min_atm_open_interest: 7"))
    assert load_universe_config(path).liquidity_screen.strict.min_atm_open_interest == 7


def test_fixture_master_is_valid() -> None:
    m = SymbolMaster.model_validate_json(FIXTURE_MASTER.read_text())
    assert {"SPY", "NVDA", "XOM", "PLTR", "UFPT"} <= set(m.symbols)
    assert "ZZZQ" not in m


def test_symbols_routine_declared() -> None:
    from arc.routines.config import load_routines
    from arc.routines.handlers import BUILTIN_HANDLERS

    assert "symbols" in BUILTIN_HANDLERS
    assert "symbols" in load_routines().jobs()


def test_symbols_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.routines import handlers

    calls: list[dict[str, Any]] = []

    def fake_refresh(cfg: SymbolMasterConfig, **kw: Any) -> SymbolMaster:
        calls.append(kw)
        return _master(tmp_path)

    monkeypatch.setattr("arc.universe.refresh_symbol_master", fake_refresh)

    from arc.store.db import connect

    class Ctx:
        settings = _settings()
        now = NOW
        options: dict[str, Any] = {}
        conn = connect(tmp_path / "arc.db")
        run_id = "run-1"
        chain_run_id = None
        recorded: list[tuple[Any, ...]] = []
        written: list[tuple[Any, ...]] = []

        def record_input(self, *a: Any, **kw: Any) -> None:
            self.recorded.append((a, kw))

        def write(self, *a: Any, **kw: Any) -> None:
            self.written.append(a)

    ctx = Ctx()
    res = handlers.symbols_source(ctx)  # type: ignore[arg-type]
    assert calls and res.metrics["symbols"] == 6  # NVDA PLTR BRK.B UFPT NOOP SPY
    assert res.metrics["optionable"] == 5
    assert ctx.recorded[0][0][0] == "symbol_master"
    # D51: the same job resolves the active list every trading day
    assert ctx.recorded[1][0][0] == "active_universe"
    assert ctx.written[0][0] == "active_universe"
    assert res.metrics["active"] == len(ctx.settings.universe)


def test_cli_check_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    rc = main(["universe", "check", "--fixture", "--json", "NVDA", "PLTR", "UFPT", "ZZZQ"])
    rows = {r["ticker"]: r for r in json.loads(capsys.readouterr().out)}
    assert rc == 1
    assert rows["NVDA"]["admitted"] and rows["PLTR"]["admitted"]
    assert rows["UFPT"]["reject"] == "illiquid"
    assert rows["ZZZQ"]["reject"] == "unknown_symbol"


def test_cli_status_missing(capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    assert main(["universe", "status"]) == 1  # hermetic cache path: missing
    assert "missing" in capsys.readouterr().out
