"""E22.4 (D97) switch-on: the shipped cash profiles trade two entry-DTE sleeves.

- cash_debit / cash_long_only ship ``short`` 10-35 (5 slots, exit block ``short``) and
  ``swing`` 36-60 (D118; was 10-29 / 30-60) (5 slots, the kind policy); margin is untouched.
- ``config/exits.yaml`` ``sleeves.short`` gives the three debit kinds a faster take
  profit (0.40), a tighter intraday stop (0.50 of the debit), an earlier profit lock
  (0.30 -> 0.10) and close_at_dte 3; a swing position keeps the D23/D78 rules.
- Baked defaults: max_open_positions 10, dedupe_executed_cooldown_sessions 2.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from arc.config import ArcSettings
from arc.exits import ExitReason, check_rules, load_exit_config, resolve_rules
from arc.models import StructureKind as SK
from arc.structures import debit_vertical, long_call

AS_OF = dt.date(2026, 10, 9)
DEBIT_KINDS = (SK.VERTICAL_DEBIT, SK.LONG_CALL, SK.LONG_PUT)


def _settings(profile: str = "cash_debit") -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ARC_ACCOUNT_PROFILE",
        "ARC_MAX_OPEN_POSITIONS",
        "ARC_DEDUPE_EXECUTED_COOLDOWN_SESSIONS",
    ):
        monkeypatch.delenv(var, raising=False)


def _rules_at_entry_dte(entry_dte: int, settings: ArcSettings | None = None) -> Any:
    """A $2.00 long call opened at *entry_dte*, bound to the policy its sleeve selects."""
    s = settings or _settings()
    st = long_call("TST", AS_OF + dt.timedelta(days=entry_dte), 100, 2.00, as_of=AS_OF)
    block = s.exit_block_of(st, AS_OF)
    return block, resolve_rules(st, load_exit_config().policy_for(st.kind, block))


# ---------------------------------------------------------------------------
# Profiles and defaults
# ---------------------------------------------------------------------------


class TestShippedProfiles:
    @pytest.mark.parametrize("profile", ["cash_debit", "cash_long_only"])
    def test_two_sleeves_five_plus_five(self, profile: str) -> None:
        s = _settings(profile)
        assert [
            (x.name, x.dte_min, x.dte_max, x.max_positions, x.exit_policy) for x in s.entry_sleeves
        ] == [("short", 10, 35, 5, "short"), ("swing", 36, 60, 5, None)]
        assert s.entry_dte_window == (10, 60)
        # the slot caps sum to the book cap
        assert sum(x.max_positions or 0 for x in s.entry_sleeves) == s.max_open_positions == 10

    @pytest.mark.parametrize(
        ("dte", "sleeve"),
        [
            (9, None),
            (10, "short"),
            (30, "short"),
            (35, "short"),
            (36, "swing"),
            (60, "swing"),
            (61, None),
        ],
    )
    def test_windows_never_overlap(self, dte: int, sleeve: str | None) -> None:
        got = _settings().sleeve_for_dte(dte)
        assert (got.name if got else None) == sleeve

    def test_margin_untouched(self) -> None:
        m = _settings("margin")
        assert not m.profile.has_sleeves
        assert (m.profile.dte_min, m.profile.dte_max) == (None, None)
        assert m.entry_dte_window == (m.dte_min, m.dte_max) == (30, 45)
        assert [x.name for x in m.entry_sleeves] == ["default"]

    def test_baked_defaults(self) -> None:
        s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.max_open_positions == 10  # D97 (was 8)
        assert s.dedupe_executed_cooldown_sessions == 2  # D97 (was 5)


# ---------------------------------------------------------------------------
# Short-sleeve exits (config/exits.yaml sleeves.short)
# ---------------------------------------------------------------------------


class TestShortSleeveExitPolicy:
    def test_block_values(self) -> None:
        cfg = load_exit_config()
        assert set(cfg.sleeves) == {"short"}
        for kind in DEBIT_KINDS:
            p = cfg.policy_for(kind, "short")
            assert p.take_profit_pct_of_debit == 0.40
            assert p.take_profit_pct_of_max_gain is None
            assert p.stop is not None
            assert (p.stop.basis, p.stop.value) == ("pct_debit", 0.50)
            assert p.stop_eod_only is False
            assert p.close_at_dte == 3
            assert p.time_adjusted_targets == []
            assert p.profit_lock is not None
            lock = p.profit_lock
            assert (lock.arm_pct, lock.floor_pct, lock.eod_only) == (0.30, 0.10, False)
            assert lock.floor_pct < lock.arm_pct < 0.40

    def test_credit_kinds_and_swing_keep_kind_policy(self) -> None:
        cfg = load_exit_config()
        for kind in (SK.VERTICAL_CREDIT, SK.IRON_CONDOR):
            assert cfg.policy_for(kind, "short") == cfg.policy_for(kind)
        for kind in DEBIT_KINDS:
            assert cfg.policy_for(kind, None) == cfg.kinds[kind]

    def test_expiry_guard_unchanged(self) -> None:
        assert load_exit_config().positions.expiry_guard.flat_by_dte == 1


class TestShortSleeveLongCall:
    """Acceptance: a short-sleeve long call at +40 % takes profit, at -50 % stops
    intraday, at DTE 3 closes; the same marks in the swing sleeve keep the D78 rules."""

    def test_short_sleeve_selected_by_entry_dte(self) -> None:
        block, _ = _rules_at_entry_dte(20)
        assert block == "short"

    def test_take_profit_at_40_pct(self) -> None:
        _, r = _rules_at_entry_dte(20)
        assert check_rules(r, pnl=0.80, dte=15, eod=False) is ExitReason.TAKE_PROFIT
        assert check_rules(r, pnl=0.79, dte=15, eod=False) is None

    def test_intraday_stop_at_minus_50_pct(self) -> None:
        _, r = _rules_at_entry_dte(20)
        assert check_rules(r, pnl=-1.00, dte=15, eod=False) is ExitReason.STOP
        assert check_rules(r, pnl=-0.99, dte=15, eod=False) is None

    def test_close_at_dte_3(self) -> None:
        _, r = _rules_at_entry_dte(20)
        assert check_rules(r, pnl=0.0, dte=3, eod=False) is ExitReason.DTE_EXIT
        assert check_rules(r, pnl=0.0, dte=4, eod=False) is None

    def test_profit_lock_30_to_10(self) -> None:
        _, r = _rules_at_entry_dte(20)
        # peak +0.60 (30 % of $2.00) armed the lock; back to +0.20 (10 %) closes
        assert check_rules(r, pnl=0.20, dte=15, eod=False, peak_pnl=0.60) is (
            ExitReason.PROFIT_LOCK
        )
        assert check_rules(r, pnl=0.21, dte=15, eod=False, peak_pnl=0.60) is None
        assert check_rules(r, pnl=0.20, dte=15, eod=False, peak_pnl=0.59) is None

    def test_swing_keeps_d78_rules(self) -> None:
        block, r = _rules_at_entry_dte(45)
        assert block is None  # DTE 45 -> swing, no exit block: the kind policy
        assert check_rules(r, pnl=0.80, dte=40, eod=False) is None  # +40 % holds
        assert check_rules(r, pnl=1.20, dte=40, eod=False) is ExitReason.TAKE_PROFIT  # 60 %
        assert check_rules(r, pnl=-1.00, dte=40, eod=False) is None  # -50 % holds
        assert check_rules(r, pnl=-1.50, dte=40, eod=False) is None  # 75 % but intraday
        assert check_rules(r, pnl=-1.50, dte=40, eod=True) is ExitReason.STOP  # EOD only
        assert check_rules(r, pnl=0.0, dte=3, eod=False) is ExitReason.DTE_EXIT
        assert check_rules(r, pnl=0.0, dte=7, eod=False) is ExitReason.DTE_EXIT
        assert check_rules(r, pnl=0.0, dte=8, eod=False) is None

    def test_dte_36_is_swing(self) -> None:
        """D118: the swing sleeve starts at 36 DTE; 30-35 now take the short block."""
        block, _ = _rules_at_entry_dte(36)
        assert block is None
        block, _ = _rules_at_entry_dte(35)
        assert block == "short"

    def test_short_debit_vertical_uses_short_block(self) -> None:
        s = _settings()
        st = debit_vertical(
            "call", "TST", AS_OF + dt.timedelta(days=20),
            long_strike=100, long_premium=3.00, short_strike=105, short_premium=1.00,
            as_of=AS_OF,
        )  # fmt: skip
        block = s.exit_block_of(st, AS_OF)
        assert block == "short"
        r = resolve_rules(st, load_exit_config().policy_for(st.kind, block))
        assert check_rules(r, pnl=0.80, dte=15, eod=False) is ExitReason.TAKE_PROFIT  # 40 % of 2
        assert check_rules(r, pnl=-1.00, dte=15, eod=False) is ExitReason.STOP
