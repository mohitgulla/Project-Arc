"""E7.9 step 5c (D84): ThetaData interest-rate parse, provider call and `arc history rates`."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from arc.backtest.marks import RateCurve
from arc.data.history.rates import format_results, merge_series, pull_rates, run_rates
from arc.data.history.thetadata import (
    RATE_FIRST_DATE,
    RATE_SYMBOLS,
    ThetaDataEodProvider,
    ThetaTerminalError,
    ThetaTier,
    parse_rate_csv,
)

FIXTURE = Path(__file__).parent / "fixtures" / "thetadata" / "rate_treasury_m3.csv"


class Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


class Session:
    def __init__(self, answer: Resp) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any], timeout: float) -> Resp:  # noqa: ARG002
        self.calls.append((url, params))
        return self.answer


def test_parse_recorded_csv() -> None:
    rows = parse_rate_csv(FIXTURE.read_text())
    assert len(rows) == 16
    assert rows[0] == (dt.date(2026, 9, 24), pytest.approx(0.0424))
    assert rows[-1] == (dt.date(2026, 10, 9), pytest.approx(0.0423))
    assert [d for d, _ in rows] == sorted(d for d, _ in rows)


def test_parse_skips_blank_and_bad_rates_and_dedupes() -> None:
    text = 'created,rate\n"2024-01-02",5.0\n"2024-01-03",\n"2024-01-04",n/a\n20240102,5.5\n'
    assert parse_rate_csv(text) == [(dt.date(2024, 1, 2), pytest.approx(0.055))]
    assert parse_rate_csv("") == []


def test_provider_fetch_interest_rate_params_and_window() -> None:
    s = Session(Resp(200, FIXTURE.read_text()))
    logs: list[Any] = []
    p = ThetaDataEodProvider(session=s, on_request=logs.append)
    got = p.fetch_interest_rate("treasury_m3", dt.date(2026, 10, 1), dt.date(2026, 10, 9))
    assert [d for d, _ in got][0] == dt.date(2026, 10, 1) and len(got) == 9
    url, params = s.calls[0]
    assert url.endswith("/v3/interest_rate/history/eod")
    assert params == {
        "symbol": "TREASURY_M3",
        "start_date": "20261001",
        "end_date": "20261009",
        "format": "csv",
    }
    assert logs[0].kind == "rate" and logs[0].rows == 16


def test_provider_no_data_is_empty_and_refusal_raises() -> None:
    p = ThetaDataEodProvider(session=Session(Resp(472)))
    assert p.fetch_interest_rate("SOFR", dt.date(2024, 1, 1), dt.date(2024, 2, 1)) == []
    refused = Resp(400, "Requesting Interest Rate data before 2024-01-01 with FREE subscription.")
    p = ThetaDataEodProvider(session=Session(refused), max_retries=0)
    with pytest.raises(ThetaTerminalError, match="before 2024-01-01"):
        p.fetch_interest_rate("SOFR", dt.date(1970, 1, 1), dt.date(2024, 2, 1))


def test_rate_constants() -> None:
    assert len(RATE_SYMBOLS) == 12 and "SOFR" in RATE_SYMBOLS and "TREASURY_M3" in RATE_SYMBOLS
    assert RATE_FIRST_DATE[ThetaTier.FREE] == dt.date(2024, 1, 1)
    assert RATE_FIRST_DATE[ThetaTier.VALUE] == dt.date(1970, 1, 1)


class FakeRates:
    def __init__(self, data: dict[str, list[tuple[dt.date, float]]], fail: set[str]) -> None:
        self.data = data
        self.fail = fail

    def fetch_interest_rate(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> list[tuple[dt.date, float]]:
        if symbol in self.fail:
            msg = "Theta Terminal HTTP 400: refused"
            raise ThetaTerminalError(msg)
        return [(d, r) for d, r in self.data.get(symbol, []) if start <= d <= end]


def test_pull_writes_series_and_treasury_3m_read_by_rate_curve(tmp_path: Path) -> None:
    m3 = parse_rate_csv(FIXTURE.read_text())
    src = FakeRates({"TREASURY_M3": m3, "SOFR": [(dt.date(2026, 10, 1), 0.04)]}, {"TREASURY_Y30"})
    res = pull_rates(
        src, tmp_path, dt.date(2024, 1, 1), dt.date(2026, 10, 9),
        ["SOFR", "TREASURY_M3", "TREASURY_Y30", "TREASURY_Y1"],
    )  # fmt: skip
    by = {r.symbol: r for r in res}
    assert by["TREASURY_M3"].rows == 16 and by["TREASURY_M3"].last == dt.date(2026, 10, 9)
    assert by["TREASURY_Y30"].error and by["TREASURY_Y30"].rows == 0
    assert by["TREASURY_Y1"].rows == 0 and by["TREASURY_Y1"].error is None
    assert not (tmp_path / "rates" / "TREASURY_Y1.parquet").exists()
    t3 = pd.read_parquet(tmp_path / "rates" / "treasury_3m.parquet")
    assert list(t3.columns) == ["date", "rate"] and len(t3) == 16
    curve = RateCurve.load(tmp_path, flat=0.04, source="treasury")
    assert curve.source == "treasury"
    assert curve.rate(dt.date(2026, 10, 9)) == pytest.approx(0.0423)
    assert curve.rate(dt.date(2020, 1, 2)) == 0.04  # before the series: flat
    assert "TREASURY_Y30" in format_results(res)


def test_rerun_merges_fresh_wins(tmp_path: Path) -> None:
    d1, d2 = dt.date(2024, 1, 2), dt.date(2024, 1, 3)
    pull_rates(FakeRates({"SOFR": [(d1, 0.05)]}, set()), tmp_path, d1, d1, ["SOFR"])
    pull_rates(FakeRates({"SOFR": [(d1, 0.051), (d2, 0.052)]}, set()), tmp_path, d1, d2, ["SOFR"])
    df = pd.read_parquet(tmp_path / "rates" / "SOFR.parquet")
    assert list(zip(df["date"], df["rate"], strict=True)) == [(d1, 0.051), (d2, 0.052)]
    assert merge_series(df.iloc[0:0], []).empty


def test_run_rates_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    m3 = parse_rate_csv(FIXTURE.read_text())
    args = argparse.Namespace(
        data_dir=tmp_path, rate_tier="free", start=None, end=dt.date(2026, 10, 9),
        symbols="treasury_m3", theta_url=None,
    )  # fmt: skip
    assert run_rates(args, source=FakeRates({"TREASURY_M3": m3}, set())) == 0
    assert "TREASURY_M3" in capsys.readouterr().out
    args.symbols = None
    assert run_rates(args, source=FakeRates({}, {"SOFR"})) == 1


def test_cli_parser_wired() -> None:
    from arc.cli import _make_parser

    ns = _make_parser().parse_args(["history", "rates", "--provider", "thetadata"])
    assert ns.history_command == "rates" and ns.rate_tier == "free" and ns.symbols is None
