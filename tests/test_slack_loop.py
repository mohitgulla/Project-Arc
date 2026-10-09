"""D36 root-line renderer (arc/slack/loop.py) — pure rendering tests."""

from __future__ import annotations

import datetime as dt

import pytest

from arc.slack.loop import HEADLINE_MAX, LoopOutcome, LoopRoot, loop_status_line, slot_stamp
from arc.utils.calendar import ET

SLOT = dt.datetime(2026, 9, 28, 9, 40, tzinfo=ET)


def _root(**kw: object) -> LoopRoot:
    base: dict[str, object] = {
        "slot": SLOT,
        "equity": 101_234.4,
        "day_pnl": 312.2,
        "orders_used": 3,
        "orders_limit": 200,
    }
    base.update(kw)
    return LoopRoot(**base)  # type: ignore[arg-type]


FACTS = "2026-09-28 09:40ET • Portfolio: $101,234 • P&L: +$312 • Orders: 3/200"


class TestRootLine:
    def test_traded_buy(self) -> None:
        r = _root(buys=["SPY"])
        assert r.outcome is LoopOutcome.TRADED
        assert r.text() == f":white_check_mark: {FACTS} • BUY: SPY"

    def test_traded_buy_and_sell(self) -> None:
        r = _root(buys=["SPY", "NVDA"], sells=["XOM"], pending=["QQQ"])
        assert r.text() == f":white_check_mark: {FACTS} • BUY: SPY, NVDA • SELL: XOM"

    def test_pending_manual_approval(self) -> None:
        r = _root(pending=["SPY"])
        assert r.outcome is LoopOutcome.PENDING
        assert r.text() == f":hourglass_flowing_sand: {FACTS} • PENDING: SPY"

    def test_working_ladder(self) -> None:
        r = _root(working=["SPY"])
        assert r.text() == f":hourglass_flowing_sand: {FACTS} • WORKING: SPY"

    def test_hold(self) -> None:
        r = _root()
        assert r.outcome is LoopOutcome.HOLD
        assert r.text() == f":heavy_multiplication_x: {FACTS} • HOLD"

    @pytest.mark.parametrize(
        ("kw", "suffix"),
        [
            ({"no_change": True}, "HOLD (skip)"),  # D65: was "HOLD (no change)"
            ({"timeout": True}, "HOLD (timeout)"),
            ({"skipped": "previous loop running"}, "HOLD (skipped: previous loop running)"),
        ],
    )
    def test_hold_reasons(self, kw: dict[str, object], suffix: str) -> None:
        status, headline = _root(**kw).text().split("\n")
        assert status.endswith(f"• {suffix}")
        assert headline.startswith("> _*") and headline.endswith("*_")  # D65 flag headline

    def test_negative_pnl_and_missing_facts(self) -> None:
        r = _root(day_pnl=-1_250.7, equity=None, orders_used=None)
        assert loop_status_line(r) == (
            ":heavy_multiplication_x: 2026-09-28 09:40ET • Portfolio: n/a • P&L: -$1,251"
            " • Orders: n/a • HOLD"
        )

    def test_slot_stamp_is_et(self) -> None:
        assert slot_stamp(dt.datetime(2026, 9, 28, 13, 40, tzinfo=dt.UTC)) == "2026-09-28 09:40ET"
        assert slot_stamp(dt.datetime(2026, 12, 1, 14, 40, tzinfo=dt.UTC)) == "2026-12-01 09:40ET"

    def test_round_trip_through_json(self) -> None:
        r = _root(buys=["SPY"])
        again = LoopRoot.model_validate(r.model_dump(mode="json"))
        assert again == r and again.text() == r.text()


class TestHeadline:
    """D65: one bold-italic headline sentence (≤2 laptop lines) under the status line."""

    def test_headline_follows_the_status_line(self) -> None:
        r = _root(buys=["SPY"], headline=["SPY iron condor x1 filled; FOMC hold is priced."])
        assert r.text() == (
            f":white_check_mark: {FACTS} • BUY: SPY\n"
            "> _*SPY iron condor x1 filled; FOMC hold is priced.*_"
        )

    def test_one_sentence_clipped_to_two_lines(self) -> None:
        r = _root(headline=["a" * 400, "b"])
        lines = r.text().split("\n")
        assert len(lines) == 2  # status + the one headline
        assert len(lines[1]) <= HEADLINE_MAX + 6 and lines[1].endswith("…*_")

    def test_markers_and_mentions_are_neutralised(self) -> None:
        r = _root(headline=["<!channel> *bold* _it_ `x` ~s~ & co"])
        line = r.text().split("\n")[1]
        assert "<!channel>" not in line and "&lt;!channel&gt;" in line
        assert line.startswith("> _*")
        inner = line[4:-2]
        assert not any(ch in inner for ch in "*_`~")

    def test_plain_hold_has_no_headline(self) -> None:
        assert "\n" not in _root().text()
