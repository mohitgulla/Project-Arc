"""E8.5 / D26: the control panel (registry, append-only store, effective config, service)."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from arc.config import ArcEnv, ArcSettings
from arc.control import cards
from arc.control.effective import (
    apply_changes,
    cost_model,
    effective_from_path,
    effective_routines,
    effective_settings,
    exit_config,
)
from arc.control.registry import (
    NEVER_TUNABLE,
    REGISTRY,
    Direction,
    Target,
    TunableError,
    direction,
    format_value,
    lookup,
    parse_value,
)
from arc.control.service import LOCAL_ACTOR, ControlService
from arc.control.store import ConfigChangeRepo, PendingRepo
from arc.models import StructureKind as SK
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from arc.utils.yamlpatch import apply_overrides

OWNER = "U0OWNER001"
OTHER = "U0STRANGER1"
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)


def base(**kw: object) -> ArcSettings:
    return ArcSettings(  # type: ignore[call-arg]
        _env_file=None,
        approver_slack_user_ids=[OWNER],
        **kw,
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def svc(conn: sqlite3.Connection, clock: Clock) -> ControlService:
    return ControlService(
        conn, base=base(), now=clock, optionable=lambda s: s != "NOOPT", is_halted=lambda: False
    )


# ---------------------------------------------------------------------------
# yamlpatch
# ---------------------------------------------------------------------------


def test_apply_overrides_is_pure_and_creates_paths() -> None:
    data = {"a": {"b": 1}, "c": [1]}
    out = apply_overrides(data, {("a", "b"): 2, ("x", "y", "z"): 3})
    assert out == {"a": {"b": 2}, "c": [1], "x": {"y": {"z": 3}}}
    assert data == {"a": {"b": 1}, "c": [1]}
    assert apply_overrides(data, None) == data


def test_apply_overrides_rejects_bad_paths() -> None:
    with pytest.raises(ValueError, match="empty"):
        apply_overrides({}, {(): 1})
    with pytest.raises(ValueError, match="not a mapping"):
        apply_overrides({"a": 5}, {("a", "b"): 1})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_every_registry_key_maps_to_a_real_setting_or_yaml_value() -> None:
    s = base()
    fields = set(ArcSettings.model_fields)
    for t in REGISTRY.values():
        if t.target is Target.SETTINGS:
            assert t.field in fields, t.key
            assert t.field not in NEVER_TUNABLE, t.key
            continue
        from arc.control.effective import raw_yaml
        from arc.control.registry import read_raw

        raw = raw_yaml(t.target)
        if t.key == "scanner.max_be_atr":  # E16.5: ships null (off); the key must exist
            assert "max_be_atr" in raw["scanner"] and read_raw(t, raw) is None, t.key
            continue
        assert read_raw(t, raw) is not None or t.key.endswith(("stop_value", "profit_lock")), t.key
    assert s.config_version is None


def test_every_setting_is_classified_tunable_or_not() -> None:
    """Config audit: a new ArcSettings field must be added to REGISTRY, NOT_EXPOSED or
    NEVER_TUNABLE, so no card ships a knob the control panel silently ignores."""
    from arc.control.registry import NOT_EXPOSED

    exposed = {t.field for t in REGISTRY.values() if t.target is Target.SETTINGS}
    unclassified = set(ArcSettings.model_fields) - exposed - set(NOT_EXPOSED) - NEVER_TUNABLE
    assert not unclassified, f"classify in arc/control/registry.py: {sorted(unclassified)}"
    assert not (set(NOT_EXPOSED) & exposed)
    assert set(NOT_EXPOSED) <= set(ArcSettings.model_fields)


def test_summary_lists_options_for_categorical_keys(svc: ControlService) -> None:
    card = cards.config_summary(svc.show(), version=svc.version())
    assert "`account_profile` = cash_debit _(options: cash_long_only | cash_debit | margin)_" in (
        card.text
    )
    assert "_(options: scanner | managed_net_ev | rorc_day | vrp)_" in card.text
    assert "_(options: intraday | eod)_" in card.text
    # the active profile's DTE window, the E6.4 knobs and routines are all in the summary
    for key in ("profiles.cash_debit.dte_min", "realloc_min_edge", "routines.scalp.cadence"):
        assert f"`{key}`" in card.text
    assert all(len(b["text"]["text"]) <= 3000 for b in card.blocks if "text" in b)


def test_new_knobs_bounds_and_direction(svc: ControlService) -> None:
    assert svc.set("realloc_max_swaps_per_day", "0", actor=OWNER, source="slack").outcome == (
        "applied"
    )  # fewer swaps is safer
    assert svc.settings().realloc_max_swaps_per_day == 0
    r = svc.set("earnings_blackout", "off", actor=OWNER, source="slack")
    assert r.pending is not None  # turning a gate rule off needs a confirm
    assert svc.set("spread_max_pct", "40%", actor=OWNER, source="slack").outcome == "refused"
    r = svc.set("positions.remaining_ev_floor", "none", actor=OWNER, source="slack")
    assert r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    from arc.control.effective import exit_config

    assert exit_config(svc.settings()).positions.remaining_ev_floor_per_bp is None


@pytest.mark.parametrize("key", ["env", "ARC_ENV", "gate_secret", "db_path", "config_version"])
def test_never_tunable(key: str) -> None:
    with pytest.raises(TunableError, match="never tunable"):
        lookup(key)


def test_unknown_key() -> None:
    with pytest.raises(TunableError, match="unknown key"):
        lookup("nope")


def test_aliases_and_patterns() -> None:
    assert lookup("per_underlying_cap").key == "max_alloc_pct"
    assert lookup("profile").key == "account_profile"
    r = lookup("routines.scalp.enabled")
    assert r.target is Target.ROUTINES and r.path == ("scalp", "enabled")
    p = lookup("profiles.cash_debit.dte_min")
    assert p.path == ("profiles", "cash_debit", "dte_min")


@pytest.mark.parametrize(
    ("key", "raw", "match"),
    [
        ("max_alloc_pct", "11%", "hard ceiling"),
        ("max_alloc_pct", "0.2%", "outside"),
        ("max_alloc_pct", "abc", "not a number"),
        ("max_alloc_pct", "nan", "finite"),
        ("max_open_positions", "2.5", "whole number"),
        ("max_open_positions", "21", "hard ceiling"),
        ("dte_min", "5", "hard ceiling"),
        ("account_profile", "yolo", "not one of"),
        ("auto_approve.paper", "maybe", "on/off"),
        ("universe", "+NOT_A_TICKER!", "ticker"),
        ("universe", "+AAA,BBB", "mix"),
        ("universe", "-ZZZZ", "not in the current list"),
        ("exits.long_call.close_at_dte", "-1", "hard ceiling"),
        ("exits.long_call.time_targets", "14", "expected"),
        ("exits.long_call.time_targets", "99:0.3", "out of range"),
        ("exits.long_call.time_targets", "14:0.3,14:0.2", "duplicate"),
        ("routines.scalp.cadence", "sometimes", "use 'every"),
        ("routines.scalp.cadence", "at 25:00", "invalid time"),
        ("dte_max", "5%", "percentage"),
        ("slippage_frac", "1.5", "outside"),
    ],
)
def test_parse_rejections(key: str, raw: str, match: str) -> None:
    with pytest.raises(TunableError, match=match):
        parse_value(lookup(key), raw, current=["SPY"])


def test_parse_values() -> None:
    assert parse_value(lookup("max_alloc_pct"), "4%") == pytest.approx(0.04)
    assert parse_value(lookup("max_alloc_pct"), "0.04") == pytest.approx(0.04)
    assert parse_value(lookup("auto_approve.paper"), "on") is True
    assert parse_value(lookup("universe"), "+nvda,-SPY", current=["SPY", "QQQ"]) == [
        "QQQ",
        "NVDA",
    ]
    assert parse_value(lookup("universe"), "spy qqq spy") == ["SPY", "QQQ"]
    assert parse_value(lookup("exits.long_call.stop_value"), "none") is None
    assert parse_value(lookup("exits.long_call.time_targets"), "7:25%,14:0.35") == [
        {"dte_lte": 7, "take_profit_pct": 0.25},
        {"dte_lte": 14, "take_profit_pct": 0.35},
    ]
    assert parse_value(lookup("exits.long_call.time_targets"), "none") == []
    assert parse_value(lookup("routines.scalp.cadence"), "Every 30m 09:00-16:00") == (
        "every 30m 09:00-16:00"
    )
    assert parse_value(lookup("routines.scalp.cadence"), "at 09:30, 12:00") == "at 09:30,12:00"
    assert parse_value(lookup("step_seconds"), "30s") == 30
    with pytest.raises(TunableError, match="not in ARC_APPROVER"):
        parse_value(lookup("approver_ids"), "U0NEWUSER1", base_list=[OWNER])
    assert parse_value(lookup("approver_ids"), f"<@{OWNER}>", base_list=[OWNER]) == [OWNER]


def test_directions() -> None:
    cap = lookup("max_alloc_pct")
    assert direction(cap, 0.05, 0.04) is Direction.SAFER
    assert direction(cap, 0.04, 0.05) is Direction.RISKIER
    assert direction(cap, 0.05, 0.05) is Direction.UNCHANGED
    stop = lookup("exits.long_call.stop_value")
    assert direction(stop, 0.75, None) is Direction.RISKIER  # no stop
    assert direction(stop, None, 0.5) is Direction.SAFER
    dte = lookup("exits.long_call.close_at_dte")
    assert direction(dte, 7, 3) is Direction.RISKIER
    prof = lookup("account_profile")
    assert direction(prof, "cash_debit", "margin") is Direction.RISKIER
    assert direction(prof, "margin", "cash_long_only") is Direction.SAFER
    assert direction(prof, "bogus", "margin") is Direction.RISKIER
    assert direction(lookup("auto_approve.live"), False, True) is Direction.RISKIER
    assert direction(lookup("auto_approve.live"), True, False) is Direction.SAFER
    uni = lookup("universe")
    assert direction(uni, ["SPY"], ["SPY", "NVDA"]) is Direction.RISKIER
    assert direction(uni, ["SPY", "NVDA"], ["SPY"]) is Direction.SAFER
    assert direction(lookup("pipeline_scan_top"), 3, 5) is Direction.NEUTRAL
    assert direction(lookup("routines.scalp.enabled"), True, False) is Direction.RISKIER
    evl = lookup("exits.long_call.stop_eval")
    assert direction(evl, "intraday", "eod") is Direction.RISKIER


def test_format_value() -> None:
    assert format_value(lookup("max_alloc_pct"), 0.05) == "5%"
    assert format_value(lookup("auto_approve.paper"), True) == "on"
    assert format_value(lookup("universe"), ["SPY", "QQQ"]) == "SPY, QQQ"
    assert format_value(lookup("universe"), []) == "none"
    assert format_value(lookup("commission"), 0.65) == "$0.65"
    assert format_value(lookup("step_seconds"), 45) == "45s"
    assert format_value(lookup("exits.long_call.stop_value"), None) == "none"
    assert (
        format_value(
            lookup("exits.long_call.time_targets"), [{"dte_lte": 14, "take_profit_pct": 0.35}]
        )
        == "14d:35%"
    )
    assert lookup("account_profile").bounds == "cash_long_only | cash_debit | margin"
    assert lookup("auto_approve.paper").bounds == "on | off"
    assert lookup("max_alloc_pct").bounds == "0.5% – 10%"


# ---------------------------------------------------------------------------
# Append-only store
# ---------------------------------------------------------------------------


def _row(repo: ConfigChangeRepo, key: str = "max_alloc_pct", new: object = 0.04) -> int:
    return repo.append(
        key=key,
        old=0.05,
        new=new,
        is_default=False,
        actor=OWNER,
        reason=None,
        at=NOW,
        source="cli",
        status="applied",
        direction="safer",
    ).id


def test_config_changes_is_append_only(conn: sqlite3.Connection) -> None:
    repo = ConfigChangeRepo(conn)
    cid = _row(repo)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE config_changes SET new = '0.09' WHERE id = ?", (cid,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM config_changes WHERE id = ?", (cid,))
    assert repo.version() == cid


def test_pending_resolves_once(conn: sqlite3.Connection) -> None:
    repo = PendingRepo(conn)
    p = repo.create(
        key="max_alloc_pct",
        old=0.05,
        new=0.06,
        is_default=False,
        kind="set",
        actor=OWNER,
        reason=None,
        source="slack",
        now=NOW,
        ttl=dt.timedelta(minutes=10),
        base_version=0,
    )
    assert len(p.code) == 6
    assert repo.resolve(p.id, outcome="confirmed", by=OWNER, now=NOW)
    assert not repo.resolve(p.id, outcome="cancelled", by=OWNER, now=NOW)
    with pytest.raises(sqlite3.DatabaseError, match="exactly once"):
        conn.execute("UPDATE config_pending SET outcome = 'cancelled' WHERE id = ?", (p.id,))
    with pytest.raises(sqlite3.DatabaseError, match="never deleted"):
        conn.execute("DELETE FROM config_pending WHERE id = ?", (p.id,))


def test_routine_runs_has_config_version(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(routine_runs)")}
    assert "config_version" in cols


# ---------------------------------------------------------------------------
# Service: owner, bounds, confirm, revert, history
# ---------------------------------------------------------------------------


def test_non_owner_refused_and_logged(svc: ControlService) -> None:
    r = svc.set("max_alloc_pct", "3%", actor=OTHER, source="slack")
    assert r.outcome == "refused"
    assert "owner only" in r.message
    assert svc.version() == 0
    for fn in (svc.confirm, svc.cancel):
        assert fn("ABCDEF", actor=OTHER, source="slack").outcome == "refused"
    assert svc.revert("max_alloc_pct", actor=OTHER, source="slack").outcome == "refused"


def test_local_cli_is_owner_but_slack_local_is_not(svc: ControlService) -> None:
    assert svc.set("max_alloc_pct", "4%", actor=LOCAL_ACTOR, source="cli").outcome == "applied"
    r = svc.set("max_alloc_pct", "3%", actor=LOCAL_ACTOR, source="slack")
    assert r.outcome == "refused"


def test_bounds_and_ceiling_refused(svc: ControlService) -> None:
    r = svc.set("max_alloc_pct", "11%", actor=OWNER, source="slack")
    assert r.outcome == "refused" and "hard ceiling" in r.message
    r = svc.set("env", "live", actor=OWNER, source="slack")
    assert r.outcome == "refused" and "never tunable" in r.message
    assert svc.version() == 0


def test_safer_applies_immediately(svc: ControlService) -> None:
    r = svc.set("max_alloc_pct", "4%", actor=OWNER, source="slack", reason="trim")
    assert r.outcome == "applied"
    assert r.direction == "safer"
    assert r.change_id == r.config_version == 1
    s = svc.settings()
    assert s.max_alloc_pct == pytest.approx(0.04)
    assert s.config_version == 1
    row = svc.changes.get(1)
    assert row is not None and row.reason == "trim" and row.source == "slack"


def test_unchanged_is_a_noop(svc: ControlService) -> None:
    r = svc.set("max_alloc_pct", "5%", actor=OWNER, source="slack")
    assert r.outcome == "unchanged"
    assert svc.version() == 0


def test_riskier_needs_confirm(svc: ControlService, clock: Clock) -> None:
    r = svc.set("max_alloc_pct", "7%", actor=OWNER, source="slack")
    assert r.outcome == "pending" and r.pending is not None
    assert svc.settings().max_alloc_pct == pytest.approx(0.05)  # not applied yet
    assert svc.version() == 0
    ok = svc.confirm(r.pending.code.lower(), actor=OWNER, source="slack")
    assert ok.outcome == "applied" and ok.direction == "riskier"
    assert svc.settings().max_alloc_pct == pytest.approx(0.07)
    row = svc.changes.get(ok.change_id or 0)
    assert row is not None and row.pending_id == r.pending.id
    again = svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert again.outcome == "refused" and "already" in again.message


def test_confirm_expires(svc: ControlService, clock: Clock) -> None:
    r = svc.set("max_alloc_pct", "7%", actor=OWNER, source="slack")
    assert r.pending is not None
    clock.now = NOW + dt.timedelta(minutes=11)
    ok = svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert ok.outcome == "refused" and "expired" in ok.message
    assert svc.settings().max_alloc_pct == pytest.approx(0.05)


def test_confirm_refused_when_value_moved(svc: ControlService) -> None:
    r = svc.set("max_alloc_pct", "7%", actor=OWNER, source="slack")
    assert r.pending is not None
    assert svc.set("max_alloc_pct", "4%", actor=OWNER, source="slack").outcome == "applied"
    ok = svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert ok.outcome == "refused" and "changed since" in ok.message
    assert svc.settings().max_alloc_pct == pytest.approx(0.04)


def test_cancel(svc: ControlService) -> None:
    r = svc.set("account_profile", "margin", actor=OWNER, source="slack")
    assert r.outcome == "pending" and r.pending is not None
    assert svc.cancel(r.pending.code, actor=OWNER, source="slack").outcome == "cancelled"
    assert svc.cancel(r.pending.code, actor=OWNER, source="slack").outcome == "refused"
    assert svc.confirm(r.pending.code, actor=OWNER, source="slack").outcome == "refused"
    assert svc.confirm("NOPE12", actor=OWNER, source="slack").outcome == "refused"


def test_auto_approve_live_requires_confirm_paper_switch_too(svc: ControlService) -> None:
    # D34 revised: per-env switches, both riskier; live is never applied in paper.
    r = svc.set("auto_approve.live", "on", actor=OWNER, source="slack")
    assert r.outcome == "pending" and r.pending is not None
    assert svc.confirm(r.pending.code, actor=OWNER, source="slack").outcome == "applied"
    s = svc.settings()
    assert s.env is ArcEnv.PAPER and s.auto_approve is False  # live switch, paper run
    assert svc.view("auto_approve.live").value is True
    r = svc.set("auto_approve.paper", "on", actor=OWNER, source="slack")
    assert r.outcome == "pending" and r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert svc.settings().auto_approve is True
    off = svc.set("auto_approve.paper", "off", actor=OWNER, source="slack")
    assert off.outcome == "applied" and off.direction == "safer"
    assert svc.settings().auto_approve is False


def test_revert_by_id_restores_prior_value(svc: ControlService) -> None:
    a = svc.set("max_alloc_pct", "4%", actor=OWNER, source="slack")
    b = svc.set("max_alloc_pct", "3%", actor=OWNER, source="slack")
    assert (a.change_id, b.change_id) == (1, 2)
    r = svc.revert("2", actor=OWNER, source="slack")  # 3% -> 4% is riskier
    assert r.outcome == "pending" and r.pending is not None
    ok = svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert ok.outcome == "reverted"
    assert svc.settings().max_alloc_pct == pytest.approx(0.04)
    row = svc.changes.get(ok.change_id or 0)
    assert row is not None and row.status == "reverted" and row.supersedes_id == 2


def test_revert_first_change_goes_back_to_default(svc: ControlService) -> None:
    svc.set("pipeline_scan_top", "3", actor=OWNER, source="slack")
    r = svc.revert("1", actor=OWNER, source="slack")
    assert r.outcome == "reverted"
    assert svc.settings().pipeline_scan_top == base().pipeline_scan_top
    assert svc.changes.active() == {}
    assert svc.diff() == []


def test_revert_by_key_and_unknown(svc: ControlService) -> None:
    svc.set("max_alloc_pct", "4%", actor=OWNER, source="slack")
    r = svc.revert("max_alloc_pct", actor=OWNER, source="slack")
    assert r.outcome == "pending"  # 4% -> 5% default is riskier
    assert svc.revert("daily_loss_halt_pct", actor=OWNER, source="slack").outcome == "unchanged"
    assert svc.revert("999", actor=OWNER, source="slack").outcome == "refused"


def test_history_order_and_diff(svc: ControlService) -> None:
    svc.set("max_alloc_pct", "4%", actor=OWNER, source="slack")
    svc.set("pipeline_scan_top", "3", actor=OWNER, source="slack")
    svc.set("max_alloc_pct", "3%", actor=OWNER, source="slack")
    ids = [c.id for c in svc.history()]
    assert ids == [3, 2, 1]
    assert [c.id for c in svc.history("max_alloc_pct")] == [3, 1]
    assert [c.id for c in svc.history(limit=1)] == [3]
    diff = {v.tunable.key: v.value for v in svc.diff()}
    assert diff == {"max_alloc_pct": pytest.approx(0.03), "pipeline_scan_top": 3}


def test_halt_flag_recorded(conn: sqlite3.Connection, clock: Clock) -> None:
    svc = ControlService(conn, base=base(), now=clock, is_halted=lambda: True)
    r = svc.set("max_alloc_pct", "4%", actor=OWNER, source="slack")
    assert r.outcome == "applied" and r.halted
    row = svc.changes.get(1)
    assert row is not None and row.halted
    assert "halted" in cards.change_card(r).text


def test_universe_optionable_check(svc: ControlService) -> None:
    r = svc.set("universe", "+NOOPT", actor=OWNER, source="slack")
    assert r.outcome == "refused" and "not optionable" in r.message
    r = svc.set("universe", "-TSLA", actor=OWNER, source="slack")
    assert r.outcome == "applied"
    assert "TSLA" not in svc.settings().universe
    r = svc.set("universe", "+TSLA", actor=OWNER, source="slack")
    assert r.outcome == "pending"  # adding a ticker is riskier


def test_approver_list_can_only_narrow(conn: sqlite3.Connection, clock: Clock) -> None:
    svc = ControlService(
        conn,
        base=ArcSettings(_env_file=None, approver_slack_user_ids=[OWNER, "U0SECOND01"]),  # type: ignore[call-arg]
        now=clock,
        is_halted=lambda: False,
    )
    r = svc.set("approver_ids", "-U0SECOND01", actor=OWNER, source="slack")
    assert r.outcome == "applied"
    assert svc.settings().approver_slack_user_ids == [OWNER]
    # the owner check uses the env list, so a narrowed list never locks the owner out
    assert svc.set("pipeline_scan_top", "4", actor="U0SECOND01", source="slack").outcome == (
        "applied"
    )
    r = svc.set("approver_ids", "+U0THIRD001", actor=OWNER, source="slack")
    assert r.outcome == "refused"


# ---------------------------------------------------------------------------
# Effective config reaches every consumer
# ---------------------------------------------------------------------------


def test_gate_sees_override(svc: ControlService) -> None:
    """max_alloc_pct lowered -> a proposal that passed now fails per_underlying_limit."""
    from tests.test_gate import cfg as gate_cfg
    from tests.test_gate import make_proposal, run

    assert run(config=gate_cfg()).passed
    svc.base = gate_cfg(approver_slack_user_ids=[OWNER])
    # the baseline bull put risks $830 on $100k (0.83%): a 0.5% cap must reject it
    assert svc.set("max_alloc_pct", "0.5%", actor=OWNER, source="slack").outcome == "applied"
    s = svc.settings()
    d = run(make_proposal(), config=s)
    assert not d.passed
    assert any(v.startswith("per_underlying_limit") for v in d.violations)


def test_profile_switch_changes_scanner_strategies(svc: ControlService) -> None:
    from arc.scanner.scan import profile_strategy_set

    before = profile_strategy_set(svc.settings())
    r = svc.set("account_profile", "margin", actor=OWNER, source="slack")
    assert r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    s = svc.settings()
    assert s.account_profile == "margin" and s.profile.name == "margin"
    after = profile_strategy_set(s)
    assert before != after
    assert any("credit" in x.value or "condor" in x.value for x in after)


def test_profile_dte_override_reaches_entry_window(svc: ControlService) -> None:
    lo, hi = svc.settings().entry_dte_window
    r = svc.set("profiles.cash_debit.dte_min", str(lo + 5), actor=OWNER, source="slack")
    assert r.outcome == "applied"
    assert svc.settings().entry_dte_window == (lo + 5, hi)
    bad = svc.set("profiles.cash_debit.dte_min", str(hi + 10), actor=OWNER, source="slack")
    assert bad.outcome == "refused"  # the profile model rejects min > max


def test_exit_and_cost_overrides(svc: ControlService) -> None:
    assert svc.set("exits.long_call.close_at_dte", "10", actor=OWNER, source="slack").outcome == (
        "applied"
    )
    r = svc.set("exits.long_call.stop_value", "none", actor=OWNER, source="slack")
    assert r.outcome == "pending" and r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    r = svc.set("slippage_frac", "0.5", actor=OWNER, source="slack")
    assert r.outcome == "applied"  # more pessimistic cost = safer
    s = svc.settings()
    cfg = exit_config(s)
    lc = cfg.kinds[SK.LONG_CALL]
    assert lc.close_at_dte == 10 and lc.stop is None
    assert cfg.kinds[SK.LONG_PUT].close_at_dte == 7  # untouched
    assert cost_model(s).slippage_frac == pytest.approx(0.5)
    # stop back on from 'none' creates a stop with the kind's basis
    r = svc.set("exits.long_call.stop_value", "0.5", actor=OWNER, source="slack")
    assert r.outcome == "applied"
    stop = exit_config(svc.settings()).kinds[SK.LONG_CALL].stop
    assert stop is not None and stop.value == pytest.approx(0.5)
    assert svc.view("exits.long_call.stop_value").value == pytest.approx(0.5)


def test_stop_eval_and_targets_round_trip(svc: ControlService) -> None:
    r = svc.set("exits.iron_condor.stop_eval", "intraday", actor=OWNER, source="slack")
    assert r.outcome == "applied"
    assert exit_config(svc.settings()).kinds[SK.IRON_CONDOR].stop_eod_only is False
    r = svc.set("exits.iron_condor.time_targets", "14:0.35", actor=OWNER, source="slack")
    assert r.outcome == "pending"
    assert r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert svc.view("exits.iron_condor.time_targets").value == [
        {"dte_lte": 14, "take_profit_pct": 0.35}
    ]


def test_routine_override(conn: sqlite3.Connection, svc: ControlService) -> None:
    r = svc.set("routines.scalp.enabled", "off", actor=OWNER, source="slack")
    assert r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    cfg = effective_routines(conn)
    assert cfg.personas["scalp"].enabled is False
    r = svc.set("routines.scalp.cadence", "every 60m 09:00-16:00", actor=OWNER, source="slack")
    assert r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    spec = effective_routines(conn).personas["scalp"]
    assert spec.every is not None and spec.window is not None
    assert svc.view("routines.scalp.cadence").value == "every 60m 09:00-16:00"
    bad = svc.set("routines.nope.enabled", "off", actor=OWNER, source="slack")
    assert bad.outcome == "refused"


def test_loop_overrides_reach_the_dispatcher_config(
    conn: sqlite3.Connection, svc: ControlService
) -> None:
    """E5.8 (D31/D36): the loop knobs are Slack-tunable and land in `RoutinesConfig.loop`."""
    import datetime as dt

    assert svc.view("loop.max_idle").value == 15  # D85 (was 30)
    assert svc.view("loop.max_runtime").value == 7
    r = svc.set("loop.max_idle", "60", actor=OWNER, source="slack")
    assert r.pending is not None  # riskier direction (fewer full runs) → confirm
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert effective_routines(conn).loop.max_idle == dt.timedelta(minutes=60)
    assert svc.view("loop.max_idle").value == 60
    # the code ceiling: a loop must fit its 10-min slot (D61: ceiling 8m)
    assert svc.set("loop.max_runtime", "9", actor=OWNER, source="slack").outcome == "refused"
    r = svc.set("loop.slack_layout", "day_thread", actor=OWNER, source="slack")
    assert r.outcome == "applied", r
    assert effective_routines(conn).loop.slack_layout.value == "day_thread"
    r = svc.set("loop.post_hold_roots", "off", actor=OWNER, source="slack")
    assert r.outcome == "applied", r
    assert effective_routines(conn).loop.post_hold_roots is False


def test_invalid_stored_override_is_skipped() -> None:
    from arc.control.store import ConfigChange

    bogus = ConfigChange(
        id=1,
        key="max_alloc_pct",
        new="not-a-number",
        actor=OWNER,
        at=NOW,
        source="cli",
        status="applied",
        direction="safer",
    )
    unknown = bogus.model_copy(update={"id": 2, "key": "gone_key", "new": 1})
    s = apply_changes(base(), {"max_alloc_pct": bogus, "gone_key": unknown}, version=2)
    assert s.max_alloc_pct == base().max_alloc_pct
    assert s.config_version == 2


def test_effective_settings_without_store_and_read_only(tmp_path: Path) -> None:
    raw = connect(":memory:")  # not migrated
    assert effective_settings(raw, base=base()).config_version == 0
    missing = tmp_path / "none.db"
    s, routines = effective_from_path(missing, base=base())
    assert s.config_version == 0 and not missing.exists()
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    ControlService(c, base=base(), now=lambda: NOW, is_halted=lambda: False).set(
        "max_alloc_pct", "4%", actor=OWNER, source="slack"
    )
    c.close()
    s, _ = effective_from_path(db, base=base())
    assert s.max_alloc_pct == pytest.approx(0.04) and s.config_version == 1


def test_dispatcher_records_config_version(conn: sqlite3.Connection, svc: ControlService) -> None:
    from arc.routines.config import load_routines
    from arc.routines.dispatcher import Dispatcher
    from arc.routines.handlers import JobResult

    svc.set("max_alloc_pct", "4%", actor=OWNER, source="slack")
    seen: dict[str, float] = {}

    def handler(ctx):  # noqa: ANN001, ANN202
        seen["cap"] = ctx.settings.max_alloc_pct
        return JobResult(summary="ok")

    routines = load_routines()
    job = next(iter(routines.sources))
    disp = Dispatcher(conn, routines, handlers={job: handler}, is_halted=lambda: False)
    disp.run_job(job, NOW, reason="manual", now=NOW, chain=False)
    row = conn.execute(
        "SELECT config_version FROM routine_runs WHERE job = ? ORDER BY started_at DESC", (job,)
    ).fetchone()
    assert row[0] == 1
    assert seen["cap"] == pytest.approx(0.04)


# ---------------------------------------------------------------------------
# Cards + CLI
# ---------------------------------------------------------------------------


def test_cards_render(svc: ControlService) -> None:
    r = svc.set("max_alloc_pct", "4%", actor=OWNER, source="slack")
    card = cards.change_card(r, actor=OWNER)
    assert card.blocks[0]["text"]["text"] == "[Control] Set: max_alloc_pct • 5% → 4% • safer"
    p = svc.set("max_alloc_pct", "6%", actor=OWNER, source="slack")
    pc = cards.change_card(p, actor=OWNER)
    actions = [b for b in pc.blocks if b["type"] == "actions"]
    assert actions and actions[0]["elements"][0]["action_id"] == cards.CONFIRM_ACTION
    assert p.pending is not None and p.pending.code in pc.text
    refused = cards.change_card(svc.set("env", "live", actor=OWNER, source="slack"))
    assert refused.blocks[0]["text"]["text"] == "[Control] Refused: env"
    summ = cards.config_summary(svc.show(), version=svc.version())
    assert "max_alloc_pct" in summ.text and "✎" in summ.text
    assert len(summ.blocks) <= 50
    detail = cards.key_detail(svc.show("max_alloc_pct"))
    assert "Hard ceiling" in detail.text
    group = cards.key_detail(svc.show("risk"))
    assert "daily_loss_halt_pct" in group.text
    assert "max_alloc_pct" in cards.diff_card(svc.diff(), version=1).text
    assert "Everything is at" in cards.diff_card([], version=0).text
    hist = cards.history_card(svc.history())
    assert "#1" in hist.text
    assert "No changes" in cards.history_card([]).text


def test_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from arc.cli import main

    db = str(tmp_path / "arc.db")

    def run(*argv: str) -> tuple[int, dict]:
        code = main(["config", *argv, "--db", db, "--json"])
        return code, json.loads(capsys.readouterr().out)

    code, out = run("set", "pipeline_scan_top", "3", "--reason", "wider menu")
    assert code == 0 and out["result"]["outcome"] == "applied"
    code, out = run("set", "max_alloc_pct", "11%")
    assert code == 2 and out["result"]["outcome"] == "refused"
    code, out = run("set", "max_alloc_pct", "3%", "--actor", OTHER)
    assert code == 2 and "owner only" in out["result"]["message"]
    code, out = run("profile", "margin")
    assert out["result"]["outcome"] == "pending"
    code, out = run("confirm", out["result"]["pending"]["code"])
    assert out["result"]["outcome"] == "applied"
    code, out = run("diff")
    keys = {k["key"] for k in out["result"]["keys"]}
    assert keys == {"pipeline_scan_top", "account_profile"}
    code, out = run("history")
    assert [c["id"] for c in out["result"]["changes"]] == [2, 1]
    code, out = run("revert", "pipeline_scan_top")
    assert out["result"]["outcome"] == "reverted"
    code, out = run("show", "pipeline_scan_top")
    assert out["result"]["keys"][0]["overridden"] is False
    code, out = run("show", "bogus_key")
    assert code == 2
    code, out = run("show")
    assert code == 0 and out["blocks"]
    assert main(["config", "keys"]) == 0
    keys = json.loads(capsys.readouterr().out)
    assert {k["key"] for k in keys} == set(REGISTRY)


def test_yaml_files_still_load_without_overrides() -> None:
    from arc.account_profiles import load_account_profiles
    from arc.backtest.costs import load_cost_model
    from arc.exits.policy import load_exit_config
    from arc.routines.config import load_routines
    from arc.scanner.rank import load_ranking_config

    load_exit_config()
    load_cost_model()
    load_account_profiles()
    load_routines()
    load_ranking_config()
    data = yaml.safe_load(Path("config/exits.yaml").read_text())
    assert "kinds" in data
    _ = D  # decimal import kept for gate helpers parity


def test_e64a_keys_reach_exits_and_ranking(svc: ControlService) -> None:
    """E6.4a: the floor window and the live Net EV floor are tunable and reach the
    effective configs the position manager and the propose step read."""
    from arc.control.effective import exit_config, ranking_config

    assert exit_config(svc.settings()).positions.remaining_ev_floor_eod_only is True
    r = svc.set("positions.remaining_ev_floor_eval", "intraday", actor=OWNER, source="slack")
    assert r.outcome == "applied"  # floor on every review is the stricter side
    assert exit_config(svc.settings()).positions.remaining_ev_floor_eod_only is False
    r = svc.set("positions.remaining_ev_floor_eval", "eod", actor=OWNER, source="slack")
    assert r.pending is not None
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert exit_config(svc.settings()).positions.remaining_ev_floor_eod_only is True

    f = ranking_config(svc.settings()).filters
    assert (f.live, f.min_managed_net_ev) == (True, 0.0)
    assert svc.set("entries.min_managed_net_ev", "5", actor=OWNER, source="slack").outcome == (
        "applied"
    )  # a higher floor is safer
    assert ranking_config(svc.settings()).filters.min_managed_net_ev == 5.0
    r = svc.set("entries.net_ev_floor_live", "off", actor=OWNER, source="slack")
    assert r.pending is not None  # switching the floor off needs a confirm
    svc.confirm(r.pending.code, actor=OWNER, source="slack")
    assert ranking_config(svc.settings()).filters.live is False
    assert svc.set("entries.min_managed_net_ev", "-60", actor=OWNER, source="slack").outcome == (
        "refused"
    )  # below the hard ceiling


# ---------------------------------------------------------------------------
# D57 (E3.5): dollar-delta cap replaces the share-count delta cap
# ---------------------------------------------------------------------------


def test_dollar_delta_and_vega_cap_tunables() -> None:
    from arc.control.registry import Risk, is_orphaned

    t = lookup("portfolio_dollar_delta_cap_pct")
    assert t.field == "portfolio_dollar_delta_cap_pct" and t.risk is Risk.UP
    assert (t.min, t.max, t.hard_ceiling, t.unit) == (0.10, 2.00, 2.00, "pct")  # D62
    v = lookup("portfolio_vega_cap_pct")
    assert v.min is not None and v.max is not None and v.min <= 0.010 <= v.max
    assert (v.max, v.hard_ceiling) == (0.02, 0.02)
    with pytest.raises(TunableError):
        lookup("portfolio_delta_cap")
    assert "portfolio_delta_cap" not in REGISTRY and is_orphaned("portfolio_delta_cap")


def test_orphaned_share_delta_cap_override_is_ignored_and_logged(
    conn: sqlite3.Connection,
) -> None:
    """A stored override on the removed `portfolio_delta_cap` cannot convert to a share of
    equity: it is logged (config.override_orphaned) and the D57 defaults stand."""
    import structlog

    repo = ConfigChangeRepo(conn)
    repo.append(
        key="portfolio_delta_cap", old=0.30, new=0.20, is_default=False, actor=OWNER,
        reason=None, at=NOW, source="slack", status="applied", direction="safer",
    )  # fmt: skip
    with structlog.testing.capture_logs() as logs:
        s = effective_settings(conn, base=base())
    assert s.portfolio_dollar_delta_cap_pct == 1.00 and s.portfolio_vega_cap_pct == 0.010
    orphaned = [e["key"] for e in logs if e["event"] == "config.override_orphaned"]
    assert set(orphaned) == {"portfolio_delta_cap"}
    assert not [e for e in logs if e["event"] == "control.override_unknown_key"]


# -- D82: parsed-YAML cache on the read path ---------------------------------------------


def test_yaml_parse_is_cached_per_file_version(
    conn: sqlite3.Connection, clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `show` parses each YAML once; an edit (new mtime) is seen on the next read."""
    import os

    import arc.control.service as service_mod
    from arc.account_profiles import DEFAULT_PROFILES_PATH

    prof = tmp_path / "account_profiles.yaml"
    prof.write_text(DEFAULT_PROFILES_PATH.read_text())
    svc = ControlService(
        conn, base=base(account_profiles_file=prof), now=clock,
        optionable=lambda s: True, is_halted=lambda: False,
    )  # fmt: skip
    parses: list[str] = []
    real = service_mod.raw_yaml

    def counting(target: object, path: object = None) -> dict[str, object]:
        parses.append(str(path))
        return real(target, path)  # type: ignore[arg-type]

    monkeypatch.setattr(service_mod, "raw_yaml", counting)
    views = svc.show()
    assert len(views) > 100
    assert len(parses) == len(set(parses))  # one parse per file, not one per key
    assert len(parses) <= 10  # a handful of config files, not ~560 key reads
    lo = svc.view("profiles.cash_debit.dte_min").value
    assert len(parses) == len(set(parses))  # still cached
    # an edit to the file is picked up (mtime changes the cache key)
    text = prof.read_text()
    assert f"dte_min: {lo}" in text
    prof.write_text(text.replace(f"dte_min: {lo}", f"dte_min: {lo + 3}", 1))
    st = prof.stat()
    os.utime(prof, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert svc.view("profiles.cash_debit.dte_min").value == lo + 3


def test_cached_yaml_is_never_mutated_by_callers(svc: ControlService) -> None:
    """Views hand out copies: mutating a returned list value can't leak into the cache."""
    keys = [k for k in svc.keys() if isinstance(svc.view(k).value, list)]  # noqa: SIM118 - a method
    assert keys, "expected at least one list-valued key"
    k = keys[0]
    first = svc.view(k).value
    first.append("__mutated__")
    assert "__mutated__" not in svc.view(k).value
    assert "__mutated__" not in (svc.view(k).default or [])
