"""Tunable registry for the D26 control panel: one source of truth, pure.

Every key the owner may change from Slack (``!arc set``) or the CLI
(``arc config set``) has exactly one :class:`Tunable` here: its type, bounds or
choices, unit, group, description, which direction is *riskier*, and a
``hard_ceiling``: a code constant on the riskier side that no runtime change may
cross (changing it needs a PR).

A tunable targets either an :class:`arc.config.ArcSettings` field
(:attr:`Target.SETTINGS`) or a path in one of the YAML configs
(``config/exits.yaml``, ``costs.yaml``, ``account_profiles.yaml``,
``routines.yaml``). YAML overrides patch the file data before the file's own
pydantic model validates it, so an override passes the same checks as the file.

Never tunable: ``ARC_ENV``, anything under ``arc/gate/`` (code), secrets
(gate secret, API keys, tokens) and paths. :data:`NEVER_TUNABLE` lists the
settings fields that are refused by name.

Pure: no I/O, no clock, no DB. Callers pass the current value where parsing
needs it (``+NVDA`` adds to the current universe).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from arc.context.ttl import parse_duration

__all__ = [
    "EXIT_KINDS",
    "NEVER_TUNABLE",
    "NEVER_TUNABLE_PATHS",
    "NOT_EXPOSED",
    "NOT_EXPOSED_PATHS",
    "REGISTRY",
    "Direction",
    "Group",
    "Risk",
    "Target",
    "Tunable",
    "TunableError",
    "ValueType",
    "direction",
    "format_value",
    "keys_in_group",
    "lookup",
    "parse_value",
]


class TunableError(ValueError):
    """A key or value the registry refuses (unknown key, bad type, out of bounds)."""


class Group(StrEnum):
    ACCOUNT = "account"
    UNIVERSE = "universe"
    RISK = "risk"
    ENTRIES = "entries"
    EXITS = "exits"
    POSITIONS = "positions"
    EXECUTION = "execution"
    COSTS = "costs"
    APPROVALS = "approvals"
    ROUTINES = "routines"
    EXPERIMENTS = "experiments"


class Target(StrEnum):
    """Where the effective value lives."""

    SETTINGS = "settings"  # an ArcSettings field
    EXITS = "exits"  # config/exits.yaml
    COSTS = "costs"  # config/costs.yaml
    PROFILES = "account_profiles"  # config/account_profiles.yaml
    ROUTINES = "routines"  # config/routines.yaml
    RANKING = "ranking"  # config/ranking.yaml (E6.4a: live Net EV floor)
    EXPERIMENTS = "experiments"  # config/experiments.yaml (E10.1: forward A/B defaults)
    UNIVERSE = "universe"  # config/universe.yaml (liquidity screens, D56)


class ValueType(StrEnum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    CHOICE = "choice"
    TICKERS = "tickers"  # list of symbols; "+NVDA,-TSLA" edits the current list
    USER_IDS = "user_ids"  # list of Slack user ids; subset of the base list only
    FLOAT_OR_NONE = "float_or_none"  # "none" clears (e.g. no stop)
    CADENCE = "cadence"  # "every 30m [HH:MM-HH:MM]" | "at HH:MM[,HH:MM...]"
    TARGETS = "targets"  # time-adjusted take-profit targets "14:0.35,7:0.25" | "none"


class Risk(StrEnum):
    """Which way a change is riskier (drives the confirm step)."""

    UP = "up"  # a higher value is riskier (None = +inf)
    DOWN = "down"  # a lower value is riskier
    ORDER = "order"  # choices are listed safest -> riskiest
    TRUE = "true"  # turning it on is riskier
    FALSE = "false"  # turning it off is riskier
    GROW = "grow"  # adding list members is riskier, removing is safer
    ANY = "any"  # direction unclear: every change needs a confirm
    NONE = "none"  # no risk direction: applies immediately


class Direction(StrEnum):
    SAFER = "safer"
    RISKIER = "riskier"
    NEUTRAL = "neutral"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class Tunable:
    """One declarative registry entry."""

    key: str
    group: Group
    type: ValueType
    description: str
    target: Target
    risk: Risk
    unit: str = ""
    # ArcSettings field (Target.SETTINGS) or the YAML path (other targets).
    field: str | None = None
    path: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    # Code constant on the riskier side (a ceiling for UP keys, a floor for DOWN).
    hard_ceiling: float | None = None
    choices: tuple[str, ...] = ()
    # Only applies when ARC_ENV equals this (per-env switches such as auto_approve.live).
    env: str | None = None
    max_items: int | None = None
    aliases: tuple[str, ...] = ()

    @property
    def bounds(self) -> str:
        """Human bounds text: ``0.5%–10%`` / ``a | b | c`` / ``on | off``."""
        if self.choices:
            return " | ".join(self.choices)
        if self.type is ValueType.BOOL:
            return "on | off"
        if self.min is None and self.max is None:
            return "-"
        lo = "-inf" if self.min is None else format_value(self, self.min)
        hi = "+inf" if self.max is None else format_value(self, self.max)
        return f"{lo} – {hi}"


# ---------------------------------------------------------------------------
# Settings fields that are never tunable (refused by name, even via alias)
# ---------------------------------------------------------------------------

NEVER_TUNABLE: frozenset[str] = frozenset(
    {
        "env",
        "arc_env",
        "gate_secret",
        "finnhub_api_key",
        "edgar_user_agent",
        "db_path",
        "account_profiles_file",
        "account_profile_spec",
        "llm_routing_file",
        "universe_config_file",
        "owner_slack_user_id",
        "config_version",
        "yaml_overrides",
        "scalp_hermes_bin",
        "ffmpeg_bin",
        # E13.11 (D56): process topology; change by env var + PR, never at runtime.
        "broker_venue",
        "broker_transport",
        # D70 (E11.3): owner decision; live auto-approve always waits for the live gate.
        "live_auto_approve_requires_gate",
    }
)

# Settings fields that are deliberately NOT runtime-tunable (plumbing, data feeds,
# ingestion, model internals). tests/test_control.py fails when a new ArcSettings
# field is in neither REGISTRY, NEVER_TUNABLE nor here, so every new knob a card
# adds gets an explicit exposed / not-exposed decision.
NOT_EXPOSED: dict[str, str] = {
    "wash_sale_days": "tax rule, not a strategy knob",
    "structure_whitelist": "account_profile decides the allowed structures",
    "quote_max_age_seconds": "data freshness guard (gate); change by PR",
    "account_max_age_seconds": "data freshness guard (gate); change by PR",
    "gate_fee_per_leg_contract": "gate fee assumption; broker schedule",
    "ticks": "exchange tick size",
    "execution_poll_seconds": "broker polling plumbing",
    "execution_cancel_confirm_seconds": "broker cancel plumbing",
    # E11.1 (D71): broker HTTP plumbing; read-only in this card, change by PR.
    "execution_broker_connect_timeout_s": "broker HTTP connect timeout (D71); change by PR",
    "execution_broker_read_timeout_s": "broker HTTP read timeout (D71); change by PR",
    "execution_unknown_submit_lookups": "client-id lookups after a submit error (D71)",
    "execution_intraday_reconcile": "safety: reconcile an unconfirmed ladder at once (D71)",
    "alpaca_data_feed": "data subscription tier",
    "alpaca_options_feed": "data subscription tier",
    "scanner_target_delta": "target inside the tunable short band",
    "scanner_long_target_delta": "target inside the tunable long band",
    "scanner_debit_short_target_delta": "target inside the tunable debit-short band",
    "scanner_risk_free_rate": "pricing input",
    "scanner_iv_lookback": "IV-rank statistics window",
    "scanner_iv_history_dir": "path",
    "iv_min_obs_rank": "IV-rank statistics guard (D55); change by PR",
    "iv_crosscheck_max_pts": "data-quality alert threshold (context data only)",
    "iv_crosscheck_max_names": "data-quality alert plumbing",
    "iv_ext_max_age_days": "context freshness (never a gate input)",
    "iv_dividend_yields": "pricing input for the IV backfill",
    "spot_max_spread_pct": "data-quality guard on spot; change by PR",
    "alpaca_data_calls_per_minute": "data API plumbing",
    "ingest_rss_feeds": "sources live in routines.yaml",
    "ingest_rss_timeout_seconds": "network plumbing",
    "ingest_youtube_channels": "sources live in routines.yaml",
    "yt_caption_grace_minutes": "ingestion plumbing",
    "yt_max_audio_minutes": "ingestion plumbing",
    "yt_max_audio_per_run": "ingestion plumbing",
    "yt_max_audio_per_slot": "ingestion plumbing",
    "yt_caption_sleep_seconds": "ingestion plumbing",
    "yt_caption_cooldown_base_minutes": "ingestion plumbing",
    "yt_caption_cooldown_max_minutes": "ingestion plumbing",
    "yt_caption_cooldown_jitter": "ingestion plumbing",
    "whisper_model": "ingestion model",
    "scalp_timeout_seconds": "LLM plumbing",
    "scout_video_chars": "LLM plumbing",
    "scout_timeout_seconds": "LLM plumbing",
    "scout_max_calls": "LLM plumbing",
    "scalp_batch_size": "LLM plumbing",
    "scalp_max_doc_chars": "LLM plumbing",
    "scalp_story_threshold": "D30 clustering internals",
    "scalp_story_window_hours": "D30 clustering internals",
    "scalp_story_batch_size": "LLM plumbing",
    "scalp_story_doc_chars": "LLM plumbing",
    "scalp_tape_max_chars": "E13.10 prompt size (the tape is capped at 1500 chars)",
    "scalp_tape_pc_bull": "E13.10 tape direction threshold; change by PR (strategy lane)",
    "scalp_tape_pc_bear": "E13.10 tape direction threshold; change by PR (strategy lane)",
    "ingest_macro_horizon_days": "ingestion plumbing",
    "ex_dividend_horizon_days": "ingestion plumbing",
    "finnhub_insider_window_days": "D46 insider detector internals (context data only)",
    "finnhub_cluster_buyers": "D46 insider detector internals (context data only)",
    "finnhub_cluster_days": "D46 insider detector internals (context data only)",
    "persona_timeout_seconds": "LLM plumbing",
    "research_prompt_max_chars": "LLM plumbing",
    "exit_block_max_chars_per_position": "LLM plumbing",
    "quant_exit_max_cases": "LLM plumbing",
    "quant_exit_case_max_chars": "LLM plumbing",
    "exit_steps_min_remaining_s": "loop plumbing",
    "pipeline_max_context_notes": "LLM context size",
}

# routines.yaml paths deliberately NOT runtime-tunable, with the reason (the YAML
# counterpart of NOT_EXPOSED; ``lookup`` refuses them like any unknown key).
NOT_EXPOSED_PATHS: dict[str, str] = {
    # D55 (E4.11): per-feed regex lists live inside the `feeds:` list (no list-index
    # paths) and there is no regex-list value type yet; a bad pattern would fail the
    # whole rss job's config load. Changed by PR (strategy lane: `Lane: fast`).
    "sources.rss.feeds[].title_exclude": "regex list inside the feeds list; change by PR",
    "sources.rss.feeds[].title_include": "regex list inside the feeds list; change by PR",
    # D56 (E13.3): the budget splits are fixed by the decision, not tunable.
    "funnel.scalp.doc_budget_split": "fixed by D56 (equal split)",
    "funnel.scout.video_budget_split": "fixed by D56 (equal split)",
    # E13.9: the quant.revise cost guard (seconds of loop budget it needs to start).
    "steps.quant.revise.min_remaining_s": "loop plumbing",
    # E13.5: measures the Cboe publish time; not a behaviour knob.
    "options_slow.publish_probe_minutes": "measurement only (Cboe publish-time probe)",
    # E13.6: options_fast plumbing (request volume, label thresholds, size guard).
    "sources.options_fast.symbol_data_markets": "request volume (one CSV per market)",
    "sources.options_fast.strikes": "tape shape (3 strikes x call/put = the BookLevel cap)",
    "options_fast.vix_flags.vix_gt_25": "tape label only (the gate's no_trade_vix_max rules)",
    "options_fast.vix_flags.vix_gt_35": "tape label only (the gate's no_trade_vix_max rules)",
}

# routines.yaml paths that are never runtime-tunable (path/limit guards; change by PR).
NEVER_TUNABLE_PATHS: frozenset[str] = frozenset({"options_fast.max_csv_bytes"})

EXIT_KINDS: tuple[str, ...] = (
    "vertical_credit",
    "iron_condor",
    "vertical_debit",
    "long_call",
    "long_put",
)
_CREDIT_EXIT_KINDS = frozenset({"vertical_credit", "iron_condor"})

PROFILE_ORDER: tuple[str, ...] = ("cash_long_only", "cash_debit", "margin")  # safest first
RANK_MENU_BY: tuple[str, ...] = ("scanner", "managed_net_ev", "rorc_day", "vrp")
STOP_BASES: tuple[str, ...] = ("pct_max_loss", "pct_debit", "credit_multiple")

MAX_UNIVERSE = 25  # D58: hard ceiling on the core list (arc.universe.tiers.MAX_CORE)
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_USER_RE = re.compile(r"^[UW][A-Z0-9]{6,20}$")
# Only `enabled` and `cadence` of a routine are tunable at runtime. `lane` (D39) and
# `tick.after_sources_wait` are process topology, edited in routines.yaml only.
_ROUTINE_KEY_RE = re.compile(r"^routines\.(?P<job>[a-z0-9_.]+)\.(?P<attr>enabled|cadence)$")
_PROFILE_KEY_RE = re.compile(r"^profiles\.(?P<name>[a-z0-9_]+)\.(?P<attr>dte_min|dte_max)$")


def _s(key: str, group: Group, typ: ValueType, desc: str, risk: Risk, **kw: Any) -> Tunable:
    return Tunable(
        key=key,
        group=group,
        type=typ,
        description=desc,
        target=Target.SETTINGS,
        risk=risk,
        field=kw.pop("field", key),
        **kw,
    )


_F, _I, _B = ValueType.FLOAT, ValueType.INT, ValueType.BOOL

_STATIC: tuple[Tunable, ...] = (
    # -- account ---------------------------------------------------------------
    _s(
        "account_profile",
        Group.ACCOUNT,
        ValueType.CHOICE,
        "Account profile (D25): which structures, net debit/credit and buying power the gate "
        "allows; also sets the scanner strategies and entry DTE window.",
        Risk.ORDER,
        choices=PROFILE_ORDER,
        aliases=("profile",),
    ),
    # D34 (revised 2026-09-28): one switch per environment; both riskier-direction, so
    # turning either on always needs the confirm code. Only the key for the running
    # ARC_ENV reaches ArcSettings.auto_approve (see arc.control.effective).
    _s(
        "auto_approve.paper",
        Group.ACCOUNT,
        _B,
        "D34: auto-approve gate-passed proposals when ARC_ENV=paper (gate, budget, halts and "
        "token+approval still apply).",
        Risk.TRUE,
        field="auto_approve",
        env="paper",
    ),
    _s(
        "auto_approve.live",
        Group.ACCOUNT,
        _B,
        "D34: auto-approve gate-passed proposals when ARC_ENV=live; turning it on needs the "
        "one-time confirm code.",
        Risk.TRUE,
        field="auto_approve",
        env="live",
    ),
    # E7.5a: the scorecard gate in front of D34 auto-approve (opens only). D70: the
    # opt-out is paper-only; a live process forces the gate on (ArcSettings validator).
    _s(
        "auto_approve.scorecard_gate",
        Group.ACCOUNT,
        _B,
        "E7.5a: auto-approve opens only when the scorecard shows enough closed trades, "
        "realised net EV >= 0 and slippage within tolerance. Off = explicit opt-out "
        "(paper only, D70: live always runs the gate).",
        Risk.FALSE,
        field="auto_approve_scorecard_gate",
        aliases=("scorecard_gate",),
        env="paper",
    ),
    _s(
        "auto_approve.min_closed_trades",
        Group.ACCOUNT,
        _I,
        "E7.5a: closed trades the scorecard gate needs before auto-approving opens (paper; "
        "live uses auto_approve.live_min_closed_trades).",
        Risk.DOWN,
        field="auto_approve_min_closed_trades",
        min=10,
        max=500,
        hard_ceiling=10,
    ),
    # D70 (E11.3): the live collection phase. Live-only keys; the riskier direction
    # (a lower threshold / a higher cap) needs the confirm code.
    _s(
        "auto_approve.live_min_closed_trades",
        Group.ACCOUNT,
        _I,
        "D70: LIVE closed trades (live store only) before live auto-approve is effective and "
        "the live size cap lifts.",
        Risk.DOWN,
        field="auto_approve_live_min_closed_trades",
        min=10,
        max=500,
        hard_ceiling=10,
        env="live",
    ),
    _s(
        "live.max_contracts_until_gate",
        Group.ACCOUNT,
        _I,
        "D70: contracts per live open until the live scorecard gate is met (sizing clamp + "
        "gate rule live_size_cap). Lifts itself when the gate is met.",
        Risk.UP,
        field="live_max_contracts_until_gate",
        min=1,
        max=5,
        hard_ceiling=5,
        unit="contracts",
        env="live",
    ),
    _s(
        "live.gate_met",
        Group.ACCOUNT,
        _B,
        "D70: the live scorecard gate was met (sticky). Turned on only by Arc (arc:live-gate) "
        "the first sweep that finds it met; lifts the live size cap and lets auto_approve.live "
        "take effect. The owner may turn it off (re-imposes the cap), never on.",
        Risk.TRUE,
        field="live_gate_met",
        env="live",
    ),
    _s(
        "auto_approve.slippage_tolerance",
        Group.ACCOUNT,
        _F,
        "E7.5a: realised entry slippage may be at most modelled half-spread x this.",
        Risk.UP,
        field="auto_approve_slippage_tolerance",
        min=0.5,
        max=3.0,
        hard_ceiling=3.0,
    ),
    _s(
        "approver_ids",
        Group.ACCOUNT,
        ValueType.USER_IDS,
        "Slack users whose Approve/Reject clicks count. Can only narrow the ARC_APPROVER_SLACK_"
        "USER_IDS list (never add a new id from Slack).",
        Risk.GROW,
        field="approver_slack_user_ids",
        max_items=10,
    ),
    # -- universe --------------------------------------------------------------
    _s(
        "universe",
        Group.UNIVERSE,
        ValueType.TICKERS,
        "Core tier (D56: 20 names, no ETFs): always scanned and accepted, no liquidity "
        "screen. The active list adds momentum and discovery (cap "
        "universe_active_max). '+NVDA,-TSLA' edits the list; added tickers must be "
        "optionable.",
        Risk.GROW,
        max_items=MAX_UNIVERSE,
    ),
    _s(
        "universe_active_max",
        Group.UNIVERSE,
        _I,
        "D56: the deduped active list (core > momentum > discovery) is capped "
        "at this many names; the rest are journaled universe:over_active_cap.",
        Risk.UP,
        min=1,
        max=60,
        hard_ceiling=60,
    ),
    _s(
        "universe_momentum_size_d56",
        Group.UNIVERSE,
        _I,
        "D56: momentum tier size (top N of the 25-row momentum feed).",
        Risk.UP,
        min=0,
        max=50,
        hard_ceiling=50,
    ),
    _s(
        "universe_discovery_size",
        Group.UNIVERSE,
        _I,
        "D58: discovery tier size (the Scout's YouTube calls, ranked).",
        Risk.UP,
        min=0,
        max=25,
        hard_ceiling=25,
    ),
    _s(
        "universe_trending_size",
        Group.UNIVERSE,
        _I,
        "D58: trending tier size (the daily Reddit + Stocktwits ranking).",
        Risk.UP,
        min=0,
        max=25,
        hard_ceiling=25,
    ),
    _s(
        "universe_floor_core",
        Group.UNIVERSE,
        _F,
        "D56: min Scalp confidence for a core name's candidate.",
        Risk.DOWN,
        min=0.30,
        max=0.95,
        hard_ceiling=0.30,
        # E13.15: the single D51 Scalp floor (and its pre-rename keys) became per tier
        aliases=("scalp_min_confidence", "sweep_min_confidence", "scout_min_confidence"),
    ),
    _s(
        "universe_floor_momentum",
        Group.UNIVERSE,
        _F,
        "D56: min Scalp confidence for a momentum name's candidate.",
        Risk.DOWN,
        min=0.30,
        max=0.95,
        hard_ceiling=0.30,
    ),
    _s(
        "universe_floor_discovery",
        Group.UNIVERSE,
        _F,
        "D56: min Scalp confidence for a discovery name's candidate (owner: >= 0.6).",
        Risk.DOWN,
        min=0.30,
        max=0.95,
        hard_ceiling=0.30,
    ),
    _s(
        "universe_floor_trending",
        Group.UNIVERSE,
        _F,
        "D58: min Scalp confidence for a trending name's candidate.",
        Risk.DOWN,
        min=0.30,
        max=0.95,
        hard_ceiling=0.30,
    ),
    _s(
        "universe_mode",
        Group.UNIVERSE,
        ValueType.CHOICE,
        "D28: strict = only the active list; seed = also any listed optionable name in a "
        "tier (core / momentum / discovery) that passes its screen (config/universe.yaml).",
        Risk.ORDER,
        choices=("strict", "seed"),
    ),
    _s(
        "scalp_doc_budget",
        Group.UNIVERSE,
        _I,
        "D30: docs the Scalp reads per run, shared equally across sources (round-robin); "
        "higher = more LLM tokens, never more trades.",
        Risk.NONE,
        min=20,
        max=400,
        hard_ceiling=400,
        aliases=("sweep_doc_budget", "scout_doc_budget"),  # D56: was sweep_*; D54: was scout_*
    ),
    # D56 (E13.4): standard (momentum) + loose (discovery) liquidity screens. Lower
    # floors / a wider spread admit more names to be looked at; the gate's spread
    # check and the scanner's contract filters still protect every order.
    Tunable(
        key="universe_screen_standard_min_price",
        group=Group.UNIVERSE,
        type=_F,
        description="D56 standard screen (momentum tier): min underlying price.",
        target=Target.UNIVERSE,
        risk=Risk.DOWN,
        path=("liquidity_screen", "standard", "min_price"),
        unit="$",
        min=1.0,
        max=100.0,
        hard_ceiling=1.0,
    ),
    Tunable(
        key="universe_screen_standard_min_adv_shares",
        group=Group.UNIVERSE,
        type=_F,
        description="D56 standard screen: min mean daily share volume (last adv_days sessions).",
        target=Target.UNIVERSE,
        risk=Risk.DOWN,
        path=("liquidity_screen", "standard", "min_adv_shares"),
        min=100_000,
        max=10_000_000,
        hard_ceiling=100_000,
    ),
    Tunable(
        key="universe_screen_standard_min_atm_open_interest",
        group=Group.UNIVERSE,
        type=_I,
        description="D56 standard screen: min call + put open interest over the 3 strikes "
        "nearest spot (expiry nearest 30 DTE).",
        target=Target.UNIVERSE,
        risk=Risk.DOWN,
        path=("liquidity_screen", "standard", "min_atm_open_interest"),
        min=25,
        max=5000,
        hard_ceiling=25,
    ),
    Tunable(
        key="universe_screen_standard_max_atm_spread_pct",
        group=Group.UNIVERSE,
        type=_F,
        description="D56 standard screen: max ATM (ask - bid) / mid, call and put averaged.",
        target=Target.UNIVERSE,
        risk=Risk.UP,
        path=("liquidity_screen", "standard", "max_atm_spread_pct"),
        unit="pct",
        min=0.02,
        max=0.40,
        hard_ceiling=0.40,
    ),
    Tunable(
        key="universe_screen_loose_min_price",
        group=Group.UNIVERSE,
        type=_F,
        description="D56 loose screen (discovery tier): min underlying price.",
        target=Target.UNIVERSE,
        risk=Risk.DOWN,
        path=("liquidity_screen", "loose", "min_price"),
        unit="$",
        min=1.0,
        max=100.0,
        hard_ceiling=1.0,
    ),
    Tunable(
        key="universe_screen_loose_min_adv_shares",
        group=Group.UNIVERSE,
        type=_F,
        description="D56 loose screen: min mean daily share volume (last adv_days sessions).",
        target=Target.UNIVERSE,
        risk=Risk.DOWN,
        path=("liquidity_screen", "loose", "min_adv_shares"),
        min=100_000,
        max=10_000_000,
        hard_ceiling=100_000,
    ),
    Tunable(
        key="universe_screen_loose_min_atm_open_interest",
        group=Group.UNIVERSE,
        type=_I,
        description="D56 loose screen: min call + put open interest over the 3 strikes "
        "nearest spot (expiry nearest 30 DTE).",
        target=Target.UNIVERSE,
        risk=Risk.DOWN,
        path=("liquidity_screen", "loose", "min_atm_open_interest"),
        min=25,
        max=5000,
        hard_ceiling=25,
    ),
    Tunable(
        key="universe_screen_loose_max_atm_spread_pct",
        group=Group.UNIVERSE,
        type=_F,
        description="D56 loose screen: max ATM (ask - bid) / mid, call and put averaged.",
        target=Target.UNIVERSE,
        risk=Risk.UP,
        path=("liquidity_screen", "loose", "max_atm_spread_pct"),
        unit="pct",
        min=0.02,
        max=0.40,
        hard_ceiling=0.40,
    ),
    # E4.8 / D46: Finnhub per-ticker context (data only; the gate never reads it).
    _s(
        "finnhub_calls_per_minute",
        Group.UNIVERSE,
        _I,
        "D46: Finnhub calls per minute shared by every Finnhub job across processes "
        "(earnings calendar included). The free key allows 60; above that Finnhub 429s.",
        Risk.UP,
        min=1,
        max=60,
        hard_ceiling=60,
    ),
    _s(
        "finnhub_max_tickers",
        Group.UNIVERSE,
        _I,
        "D46/D51: tickers per Finnhub context run (open-position underlyings first, then "
        "today's candidates, core, momentum, discovery). Each ticker is one call per job, "
        "against the per-minute budget.",
        Risk.NONE,
        min=1,
        max=200,
        hard_ceiling=200,
    ),
    # -- risk --------------------------------------------------------------------
    _s(
        "max_alloc_pct",
        Group.RISK,
        _F,
        "D18 cap: max loss per underlying (existing + new) as a share of equity; also the "
        "sizing cap. Gate rule per_underlying_limit.",
        Risk.UP,
        unit="pct",
        min=0.005,
        max=0.10,
        hard_ceiling=0.10,
        aliases=("per_underlying_cap",),
    ),
    _s(
        "daily_loss_halt_pct",
        Group.RISK,
        _F,
        "Day loss vs previous close that blocks new entries and halts (gate rule daily_loss).",
        Risk.UP,
        unit="pct",
        min=0.005,
        max=0.05,
        hard_ceiling=0.05,
    ),
    _s(
        "max_open_positions",
        Group.RISK,
        _I,
        "Max simultaneous open positions (gate rule max_open_positions).",
        Risk.UP,
        min=1,
        max=20,
        hard_ceiling=20,
    ),
    _s(
        "portfolio_dollar_delta_cap_pct",
        Group.RISK,
        _F,
        "D57/D62: |net dollar delta| (Σ Δ share-eq × spot) cap as a share of equity "
        "(gate rule greek_caps).",
        Risk.UP,
        unit="pct",
        min=0.10,
        max=2.00,
        hard_ceiling=2.00,
    ),
    _s(
        "portfolio_beta_delta_cap_pct",
        Group.RISK,
        _F,
        "D62: |beta-weighted net dollar delta| (Σ Δ share-eq × spot × max(β vs SPY, 1)) "
        "cap as a share of equity (gate rule greek_caps).",
        Risk.UP,
        unit="pct",
        min=0.25,
        max=4.00,
        hard_ceiling=4.00,
    ),
    _s(
        "portfolio_vega_cap_pct",
        Group.RISK,
        _F,
        "|net vega| cap per vol point as a share of equity (gate rule greek_caps).",
        Risk.UP,
        unit="pct",
        min=0.001,
        max=0.02,
        hard_ceiling=0.02,
    ),
    _s(
        "earnings_blackout",
        Group.RISK,
        _B,
        "Gate rule earnings_blackout: no new debit entries spanning the next earnings date.",
        Risk.FALSE,
    ),
    # -- entries -----------------------------------------------------------------
    _s(
        "dte_min",
        Group.ENTRIES,
        _I,
        "Global entry DTE floor (used when the account profile sets no window; margin).",
        Risk.DOWN,
        min=7,
        max=120,
        hard_ceiling=7,
    ),
    _s(
        "dte_max",
        Group.ENTRIES,
        _I,
        "Global entry DTE cap (used when the account profile sets no window; margin).",
        Risk.NONE,
        min=7,
        max=120,
    ),
    _s(
        "short_delta_min",
        Group.ENTRIES,
        _F,
        "Credit short-strike |delta| band, lower bound.",
        Risk.UP,
        field="scanner_short_delta_min",
        min=0.05,
        max=0.45,
        hard_ceiling=0.45,
    ),
    _s(
        "short_delta_max",
        Group.ENTRIES,
        _F,
        "Credit short-strike |delta| band, upper bound.",
        Risk.UP,
        field="scanner_short_delta_max",
        min=0.05,
        max=0.45,
        hard_ceiling=0.45,
    ),
    _s(
        "long_delta_min",
        Group.ENTRIES,
        _F,
        "Debit long-leg |delta| band, lower bound (lower = further OTM).",
        Risk.DOWN,
        field="scanner_long_delta_min",
        min=0.15,
        max=0.90,
        hard_ceiling=0.15,
    ),
    _s(
        "long_delta_max",
        Group.ENTRIES,
        _F,
        "Debit long-leg |delta| band, upper bound.",
        Risk.NONE,
        field="scanner_long_delta_max",
        min=0.15,
        max=0.95,
    ),
    _s(
        "wing_width",
        Group.ENTRIES,
        _F,
        "Target credit wing width in dollars (wider = larger max loss per contract).",
        Risk.UP,
        field="scanner_wing_width",
        unit="$",
        min=1.0,
        max=25.0,
        hard_ceiling=25.0,
    ),
    _s(
        "pipeline_scan_top",
        Group.ENTRIES,
        _I,
        "Scanner candidates per ticker offered to the Quant.",
        Risk.NONE,
        min=1,
        max=20,
    ),
    _s(
        "debit_short_delta_min",
        Group.ENTRIES,
        _F,
        "Debit-vertical short-leg |delta| band, lower bound (E3.4, cash_debit).",
        Risk.NONE,
        field="scanner_debit_short_delta_min",
        min=0.05,
        max=0.60,
    ),
    _s(
        "debit_short_delta_max",
        Group.ENTRIES,
        _F,
        "Debit-vertical short-leg |delta| band, upper bound (E3.4, cash_debit).",
        Risk.NONE,
        field="scanner_debit_short_delta_max",
        min=0.05,
        max=0.60,
    ),
    _s(
        "debit_width",
        Group.ENTRIES,
        ValueType.FLOAT_OR_NONE,
        "Target debit-vertical width in dollars; 'none' = pick the short leg by delta only.",
        Risk.ANY,
        field="scanner_debit_width",
        unit="$",
        min=1.0,
        max=25.0,
    ),
    _s(
        "spread_max_pct",
        Group.ENTRIES,
        _F,
        "Liquidity: max leg bid-ask spread as a share of mid (scanner filter + gate rule \
liquidity; a leg passes if within this OR spread_max_abs).",
        Risk.UP,
        unit="pct",
        min=0.02,
        max=0.25,
        hard_ceiling=0.25,
    ),
    _s(
        "spread_max_abs",
        Group.ENTRIES,
        _F,
        "Liquidity: max leg bid-ask spread in dollars (OR with spread_max_pct).",
        Risk.UP,
        unit="$",
        min=0.01,
        max=0.50,
        hard_ceiling=0.50,
    ),
    _s(
        "min_open_interest",
        Group.ENTRIES,
        _I,
        "Liquidity: scanner drops contracts with open interest below this.",
        Risk.DOWN,
        field="scanner_min_open_interest",
        min=10,
        max=5000,
        hard_ceiling=10,
    ),
    _s(
        "min_volume",
        Group.ENTRIES,
        _I,
        "Liquidity: scanner drops contracts with day volume below this.",
        Risk.DOWN,
        field="scanner_min_volume",
        min=1,
        max=1000,
        hard_ceiling=1,
    ),
    _s(
        "max_shortlist",
        Group.ENTRIES,
        _I,
        "Quant/Risk budget: the first N Research-ranked tickers get a structure (D28).",
        Risk.UP,
        field="pipeline_max_shortlist",
        min=1,
        max=10,
        hard_ceiling=10,
    ),
    Tunable(
        key="rank_by",
        group=Group.ENTRIES,
        type=ValueType.CHOICE,
        description="Quant menu order (exits.yaml pipeline.rank_menu_by; E7.5 kept 'scanner').",
        target=Target.EXITS,
        risk=Risk.NONE,
        path=("pipeline", "rank_menu_by"),
        choices=RANK_MENU_BY,
    ),
    # -- positions (E6.4, D19): early exits + close-to-reallocate -----------------
    Tunable(
        key="positions.remaining_ev_floor",
        group=Group.POSITIONS,
        type=ValueType.FLOAT_OR_NONE,
        description="Suggest closing once remaining net EV per $ of buying power held drops "
        "below this (-0.01 = expected to lose >1% of that BP vs closing now); 'none' = off.",
        target=Target.EXITS,
        risk=Risk.ANY,
        path=("positions", "remaining_ev_floor_per_bp"),
        unit="pct",
        min=-0.10,
        max=0.05,
    ),
    Tunable(
        key="positions.remaining_ev_floor_eval",
        group=Group.POSITIONS,
        type=ValueType.CHOICE,
        description="E6.4a: evaluate the remaining-EV floor on end-of-day marks only (eod, "
        "never on the fill day's intraday reviews) or on every review (intraday).",
        target=Target.EXITS,
        risk=Risk.ORDER,
        path=("positions", "remaining_ev_floor_eod_only"),
        choices=("intraday", "eod"),  # eod (relaxed, like the D23 stop) is the riskier side
    ),
    # -- expiry guard (E11.4, D73) ------------------------------------------------
    Tunable(
        key="expiry_guard.flat_by_dte",
        group=Group.POSITIONS,
        type=ValueType.INT,
        description="E11.4: every structure must be closed by the end of the session this many "
        "calendar days before expiry (1 = the day before); lowering it is riskier.",
        target=Target.EXITS,
        risk=Risk.DOWN,
        path=("positions", "expiry_guard", "flat_by_dte"),
        unit="d",
        min=0,
        max=5,
    ),
    Tunable(
        key="expiry_guard.attempts_per_day",
        group=Group.POSITIONS,
        type=ValueType.INT,
        description="E11.4: close proposals per structure per ET day inside the closing window "
        "(DTE <= flat_by_dte + 1); outside it, one a day.",
        target=Target.EXITS,
        risk=Risk.DOWN,
        path=("positions", "expiry_guard", "attempts_per_day"),
        min=1,
        max=8,
    ),
    Tunable(
        key="expiry_guard.dne",
        group=Group.POSITIONS,
        type=ValueType.CHOICE,
        description="E11.4: do-not-exercise at the expiry-day cutoff (paper only): never, "
        "near_money (|spot - strike| <= pin_band) or all_longs.",
        target=Target.EXITS,
        risk=Risk.ANY,
        path=("positions", "expiry_guard", "dne"),
        choices=("never", "near_money", "all_longs"),
    ),
    Tunable(
        key="expiry_guard.pin_band",
        group=Group.POSITIONS,
        type=ValueType.FLOAT,
        description="E11.4: near_money DNE band, $ per share around the strike.",
        target=Target.EXITS,
        risk=Risk.ANY,
        path=("positions", "expiry_guard", "pin_band"),
        unit="$",
        min=0.0,
        max=5.0,
    ),
    Tunable(
        key="entries.min_managed_net_ev",
        group=Group.ENTRIES,
        type=ValueType.FLOAT,
        description="E6.4a: the live propose step rejects a structure whose managed Net EV "
        "(E2.4 managed exits, after all costs, $ per unit) is <= this (lower = more trades).",
        target=Target.RANKING,
        risk=Risk.DOWN,
        path=("ranking", "filters", "min_managed_net_ev"),
        unit="$",
        min=-50.0,
        max=500.0,
        hard_ceiling=-50.0,
    ),
    Tunable(
        key="entries.net_ev_floor_live",
        group=Group.ENTRIES,
        type=ValueType.BOOL,
        description="E6.4a: apply entries.min_managed_net_ev in the live propose step. Off = "
        "the floor is backtest-only and a negative-EV structure can be proposed.",
        target=Target.RANKING,
        risk=Risk.FALSE,
        path=("ranking", "filters", "live"),
    ),
    _s(
        "realloc_min_edge",
        Group.POSITIONS,
        _F,
        "Close-to-reallocate: a swap needs net edge (EV per $ BP, after switching costs) of "
        "at least this share of the larger |EV per BP| (lower = more swaps).",
        Risk.DOWN,
        unit="pct",
        min=0.05,
        max=2.0,
        hard_ceiling=0.05,
    ),
    _s(
        "realloc_pop_tolerance",
        Group.POSITIONS,
        _F,
        "Close-to-reallocate: the new trade's PoP may be this much below the open's remaining PoP.",
        Risk.UP,
        unit="pct",
        min=0.0,
        max=0.20,
        hard_ceiling=0.20,
    ),
    _s(
        "realloc_max_swaps_per_day",
        Group.POSITIONS,
        _I,
        "Churn limit: swaps suggested per ET day, all tickers (0 = reallocation off).",
        Risk.UP,
        min=0,
        max=10,
        hard_ceiling=10,
    ),
    _s(
        "realloc_max_swaps_per_ticker_per_day",
        Group.POSITIONS,
        _I,
        "Churn limit: swaps per ticker (closed or opened) per ET day.",
        Risk.UP,
        min=0,
        max=3,
        hard_ceiling=3,
    ),
    # E13.18 (D56): Risk exit review
    _s(
        "exit_review_max_consecutive_holds",
        Group.POSITIONS,
        _I,
        "Research exit path: a Risk `hold` on a profit-target / EV-floor signal is honoured "
        "for at most this many consecutive reviews; the next review closes it.",
        Risk.UP,
        min=1,
        max=10,
        hard_ceiling=10,
    ),
    # -- execution (D24, E6.2) ---------------------------------------------------
    _s(
        "improvement_steps",
        Group.EXECUTION,
        _I,
        "Price-improvement steps after the mid attempt (more = more concession).",
        Risk.UP,
        field="execution_improvement_steps",
        min=0,
        max=6,
        hard_ceiling=6,
    ),
    _s(
        "step_seconds",
        Group.EXECUTION,
        _I,
        "Seconds each ladder attempt works before it is cancelled.",
        Risk.NONE,
        field="execution_step_seconds",
        unit="s",
        min=10,
        max=300,
    ),
    _s(
        "band_reach",
        Group.EXECUTION,
        _F,
        "How far toward the far touch the band's worst price may go (1.0 = far touch).",
        Risk.UP,
        field="execution_band_reach",
        min=0.0,
        max=1.0,
        hard_ceiling=1.0,
    ),
    _s(
        "max_quote_age",
        Group.EXECUTION,
        _I,
        "D34: seconds between the proposal's pricing and the ladder's first attempt after "
        "which the Broker re-prices at mid (a mid outside the gate band is not sent).",
        Risk.UP,
        field="execution_max_quote_age_seconds",
        unit="s",
        min=0,
        max=600,
        hard_ceiling=600,
        aliases=("execute.max_quote_age",),
    ),
    # -- close quote check (E6.2a) -------------------------------------------------
    _s(
        "close_quote.max_age_seconds",
        Group.EXECUTION,
        _I,
        "Close legs: max quote age, from the quote's own timestamp (older = no close).",
        Risk.UP,
        field="close_quote_max_age_seconds",
        unit="s",
        min=5,
        max=300,
        hard_ceiling=300,
    ),
    _s(
        "close_quote.max_skew_seconds",
        Group.EXECUTION,
        _I,
        "Close legs: max gap between the legs' quote timestamps.",
        Risk.UP,
        field="close_quote_max_skew_seconds",
        unit="s",
        min=0,
        max=300,
        hard_ceiling=300,
    ),
    _s(
        "close_quote.max_spread_pct",
        Group.EXECUTION,
        _F,
        "Close legs: max bid-ask spread as % of mid (a leg also passes under the $ cap).",
        Risk.UP,
        field="close_quote_max_spread_pct",
        unit="pct",
        min=0.01,
        max=0.50,
        hard_ceiling=0.50,
    ),
    _s(
        "close_quote.max_spread_abs",
        Group.EXECUTION,
        _F,
        "Close legs: max bid-ask spread in $ (OR with the % cap).",
        Risk.UP,
        field="close_quote_max_spread_abs",
        unit="$",
        min=0.0,
        max=0.50,
        hard_ceiling=0.50,
    ),
    _s(
        "close_quote.max_curve_dev",
        Group.EXECUTION,
        _F,
        "Close: max $/share gap between the combo mid and the strike-curve value "
        "(indicative-feed jitter guard).",
        Risk.UP,
        field="close_quote_max_curve_dev",
        unit="$",
        min=0.01,
        max=0.50,
        hard_ceiling=0.50,
    ),
    _s(
        "close_quote.alert_after",
        Group.EXECUTION,
        _I,
        "Alert #arc-investor after this many consecutive unusable-quote close tries.",
        Risk.NONE,
        field="close_quote_alert_after",
        min=1,
        max=20,
    ),
    # -- daily options order budget (E6.5, D32) --------------------------------
    _s(
        "order_budget.daily_max",
        Group.EXECUTION,
        _I,
        "Hard cap on broker option orders per ET day (ladder attempts, opens and closes).",
        Risk.UP,
        field="order_budget_daily_max",
        min=1,
        max=200,
        hard_ceiling=200,
        aliases=("order_budget", "daily_order_max"),
    ),
    _s(
        "order_budget.restrict_at",
        Group.EXECUTION,
        _I,
        "Orders used at which selection turns restrictive.",
        Risk.UP,
        field="order_budget_restrict_at",
        min=0,
        max=200,
        hard_ceiling=200,
    ),
    _s(
        "order_budget.close_reserve",
        Group.EXECUTION,
        _I,
        "Orders kept for closes: opens stop at daily_max - reserve.",
        Risk.DOWN,
        field="order_budget_close_reserve",
        min=0,
        max=199,
        hard_ceiling=0,
    ),
    _s(
        "order_budget.restrictive.research_max_shortlist",
        Group.EXECUTION,
        _I,
        "Restrictive tier: Research shortlist cap.",
        Risk.UP,
        field="order_budget_restrictive_research_max_shortlist",
        min=0,
        max=10,
        hard_ceiling=10,
        aliases=("order_budget.restrictive.director_max_shortlist",),  # D56: was director_*
    ),
    _s(
        "order_budget.restrictive.max_new_opens_per_loop",
        Group.EXECUTION,
        _I,
        "Restrictive tier: new open proposals per pipeline run.",
        Risk.UP,
        field="order_budget_restrictive_max_new_opens_per_loop",
        min=0,
        max=10,
        hard_ceiling=10,
    ),
    _s(
        "order_budget.restrictive.min_net_ev_multiplier",
        Group.EXECUTION,
        _F,
        "Restrictive tier: managed Net EV must clear this x max(base floor, round-trip cost).",
        Risk.DOWN,
        field="order_budget_restrictive_min_net_ev_multiplier",
        min=1.0,
        max=10.0,
        hard_ceiling=1.0,
    ),
    _s(
        "order_budget.restrictive.min_pop_delta_pp",
        Group.EXECUTION,
        _F,
        "Restrictive tier: managed PoP must clear the breakeven PoP by this many points.",
        Risk.DOWN,
        field="order_budget_restrictive_min_pop_delta_pp",
        unit="pp",
        min=0.0,
        max=50.0,
        hard_ceiling=0.0,
    ),
    _s(
        "order_budget.restrictive.max_improvement_steps",
        Group.EXECUTION,
        _I,
        "Restrictive tier: ladder improvement-step cap.",
        Risk.UP,
        field="order_budget_restrictive_max_improvement_steps",
        min=0,
        max=6,
        hard_ceiling=6,
    ),
    _s(
        "order_budget.restrictive.dedupe_cooldown_multiplier",
        Group.EXECUTION,
        _F,
        "Restrictive tier: E5.9 dedupe cooldown multiplier.",
        Risk.DOWN,
        field="order_budget_restrictive_dedupe_cooldown_multiplier",
        min=1.0,
        max=10.0,
        hard_ceiling=1.0,
    ),
    # -- portfolio-aware Research, dedupe, no-trade guard (E5.9, D33) ----------
    _s(
        "dedupe.executed_cooldown",
        Group.ENTRIES,
        _I,
        "D33: sessions an executed idea (open, or closed this recently) stays suppressed.",
        Risk.DOWN,
        field="dedupe_executed_cooldown_sessions",
        unit="sessions",
        min=0,
        max=60,
        hard_ceiling=0,
    ),
    _s(
        "dedupe.proposed_cooldown",
        Group.ENTRIES,
        _I,
        "D33: sessions a proposed (pending / expired) idea stays suppressed.",
        Risk.DOWN,
        field="dedupe_proposed_cooldown_sessions",
        unit="sessions",
        min=0,
        max=60,
        hard_ceiling=0,
    ),
    _s(
        "dedupe.rejected_cooldown",
        Group.ENTRIES,
        _I,
        "D33: sessions an owner-rejected idea stays suppressed.",
        Risk.DOWN,
        field="dedupe_rejected_cooldown_sessions",
        unit="sessions",
        min=0,
        max=60,
        hard_ceiling=0,
    ),
    _s(
        "dedupe.reprice_move_pct",
        Group.ENTRIES,
        _F,
        "D33: spot move (fraction) since the last idea that re-admits a suppressed one.",
        Risk.DOWN,
        field="dedupe_reprice_move_pct",
        min=0.0,
        max=1.0,
        hard_ceiling=0.0,
    ),
    _s(
        "portfolio.sector_max_pct",
        Group.RISK,
        _F,
        "D33: sector share of open max loss that flags over_concentrated_sector.",
        Risk.UP,
        field="portfolio_sector_max_pct",
        min=0.05,
        max=1.0,
        hard_ceiling=1.0,
    ),
    _s(
        "portfolio.stance_max_pct",
        Group.RISK,
        _F,
        "D33: stance share of open max loss that flags stance_skew.",
        Risk.UP,
        field="portfolio_stance_max_pct",
        min=0.05,
        max=1.0,
        hard_ceiling=1.0,
    ),
    _s(
        "portfolio.expiry_max_pct",
        Group.RISK,
        _F,
        "D33: expiry-bucket share of open max loss that flags expiry_cluster.",
        Risk.UP,
        field="portfolio_expiry_max_pct",
        min=0.05,
        max=1.0,
        hard_ceiling=1.0,
    ),
    _s(
        "portfolio.greek_near_cap_pct",
        Group.RISK,
        _F,
        "D33: |$delta| / |beta $delta| / |vega| cap usage that flags *_near_cap (D62).",
        Risk.UP,
        field="portfolio_greek_near_cap_pct",
        min=0.05,
        max=1.0,
        hard_ceiling=1.0,
    ),
    _s(
        "portfolio.context_max_positions",
        Group.ENTRIES,
        _I,
        "D33: open positions rendered in full for Research (largest first).",
        Risk.NONE,
        field="portfolio_context_max_positions",
        min=1,
        max=50,
    ),
    _s(
        "no_trade.vix_max",
        Group.ENTRIES,
        _F,
        "D33: VIX at or above this blocks new opens (market_unclear); exits unaffected.",
        Risk.UP,
        field="no_trade_vix_max",
        min=5.0,
        max=200.0,
        hard_ceiling=200.0,
    ),
    _s(
        "no_trade.on_backwardation",
        Group.ENTRIES,
        _B,
        "D33: VIX term structure in backwardation blocks new opens.",
        Risk.FALSE,
        field="no_trade_on_backwardation",
    ),
    _s(
        "no_trade.transitional_min_confidence",
        Group.ENTRIES,
        _F,
        "D33: SPY regime stickiness below this = transitional regime, no new opens.",
        Risk.DOWN,
        field="no_trade_transitional_min_confidence",
        min=0.0,
        max=1.0,
        hard_ceiling=0.0,
    ),
    _s(
        "no_trade.require_vix",
        Group.ENTRIES,
        _B,
        "D33: no VIX reading = no new opens (market_data_missing, fail closed).",
        Risk.FALSE,
        field="no_trade_require_vix",
    ),
    # -- costs (E6.1a); fees are read-only (broker schedule) --------------------
    Tunable(
        key="slippage_frac",
        group=Group.COSTS,
        type=_F,
        description="Assumed fill: mid ± this × spread (lower = more optimistic Net EV).",
        target=Target.COSTS,
        risk=Risk.DOWN,
        path=("costs", "slippage_frac"),
        min=0.0,
        max=1.0,
        hard_ceiling=0.0,
    ),
    Tunable(
        key="commission",
        group=Group.COSTS,
        type=_F,
        description="Commission per contract assumed by the cost model ($).",
        target=Target.COSTS,
        risk=Risk.DOWN,
        path=("costs", "commission_per_contract"),
        unit="$",
        min=0.0,
        max=5.0,
        hard_ceiling=0.0,
    ),
    # -- approvals ---------------------------------------------------------------
    _s(
        "approval_ttl_seconds",
        Group.APPROVALS,
        _I,
        "How long an approval card stays actionable (gate rule approval_ttl).",
        Risk.UP,
        unit="s",
        min=60,
        max=3600,
        hard_ceiling=3600,
        aliases=("approval_ttl",),
    ),
    # D34: per-env like auto_approve; the env var only sets the paper value.
    _s(
        "auto_exit_defined_risk.paper",
        Group.APPROVALS,
        _B,
        "D24: fired exits on defined-risk positions skip the Slack approval when "
        "ARC_ENV=paper (gate still applies). Off = every exit needs an approval.",
        Risk.TRUE,
        field="auto_exit_defined_risk",
        env="paper",
        aliases=("auto_exit_defined_risk",),
    ),
    _s(
        "auto_exit_defined_risk.live",
        Group.APPROVALS,
        _B,
        "D24: fired exits on defined-risk positions skip the Slack approval when "
        "ARC_ENV=live; turning it on needs the one-time confirm code.",
        Risk.TRUE,
        field="auto_exit_defined_risk",
        env="live",
    ),
)


def _exit_tunables() -> tuple[Tunable, ...]:
    out: list[Tunable] = []
    for kind in EXIT_KINDS:
        credit = kind in _CREDIT_EXIT_KINDS
        base = ("kinds", kind)
        tp_field = "take_profit_pct_of_max_gain" if credit else "take_profit_pct_of_debit"
        tp_desc = "share of max gain" if credit else "multiple of the debit paid"
        out += [
            Tunable(
                key=f"exits.{kind}.take_profit",
                group=Group.EXITS,
                type=_F,
                description=f"{kind}: take profit at this {tp_desc} (higher = held longer).",
                target=Target.EXITS,
                risk=Risk.UP,
                path=(*base, tp_field),
                unit="pct",
                min=0.10,
                max=1.0 if credit else 3.0,
                hard_ceiling=1.0 if credit else 3.0,
            ),
            Tunable(
                key=f"exits.{kind}.stop_value",
                group=Group.EXITS,
                type=ValueType.FLOAT_OR_NONE,
                description=f"{kind}: stop level in units of the stop basis; 'none' = no stop.",
                target=Target.EXITS,
                risk=Risk.UP,
                path=(*base, "stop", "value"),
                min=0.10,
                max=3.0 if credit else 1.0,
                hard_ceiling=3.0 if credit else 1.0,
            ),
            Tunable(
                key=f"exits.{kind}.stop_basis",
                group=Group.EXITS,
                type=ValueType.CHOICE,
                description=f"{kind}: what the stop value multiplies.",
                target=Target.EXITS,
                risk=Risk.ANY,
                path=(*base, "stop", "basis"),
                choices=("pct_max_loss", "credit_multiple") if credit else ("pct_debit",),
            ),
            Tunable(
                key=f"exits.{kind}.stop_eval",
                group=Group.EXITS,
                type=ValueType.CHOICE,
                description=f"{kind}: evaluate the stop on end-of-day marks or intraday.",
                target=Target.EXITS,
                risk=Risk.ORDER,
                path=(*base, "stop_eod_only"),
                choices=("intraday", "eod"),  # eod (relaxed, D23) is the riskier side
            ),
            Tunable(
                key=f"exits.{kind}.close_at_dte",
                group=Group.EXITS,
                type=_I,
                description=f"{kind}: close once DTE <= this (lower = held closer to expiry).",
                target=Target.EXITS,
                risk=Risk.DOWN,
                path=(*base, "close_at_dte"),
                unit="d",
                min=0,
                max=60,
                hard_ceiling=0,
            ),
            Tunable(
                key=f"exits.{kind}.time_targets",
                group=Group.EXITS,
                type=ValueType.TARGETS,
                description=f"{kind}: time-adjusted take-profit '<dte>:<pct>,...' (D19); "
                "'none' clears.",
                target=Target.EXITS,
                risk=Risk.ANY,
                path=(*base, "time_adjusted_targets"),
                max_items=6,
            ),
        ]
    return tuple(out)


# D31 / D36: the trading loop's knobs live under `loop:` in routines.yaml (not per
# job), so they get plain-path tunables. Durations are minutes in the registry and
# `"<n>m"` strings in the file (write_raw / read_raw convert).
_LOOP_TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        key="loop.max_idle",
        group=Group.ROUTINES,
        type=_I,
        description="D31: minutes of unchanged inputs after which the loop runs the LLM "
        "steps again anyway (a higher value = fewer full runs).",
        target=Target.ROUTINES,
        risk=Risk.UP,
        path=("loop", "max_idle"),
        unit="m",
        min=5,
        max=240,
        hard_ceiling=240,
    ),
    Tunable(
        key="loop.max_runtime",
        group=Group.ROUTINES,
        type=_I,
        description="D31: minutes a loop chain may run before later steps are skipped "
        "(must stay under the 10-min slot; D61: 7m, ceiling 8m).",
        target=Target.ROUTINES,
        risk=Risk.UP,
        path=("loop", "max_runtime"),
        unit="m",
        min=1,
        max=8,
        hard_ceiling=8,
    ),
    Tunable(
        key="loop.pnl_bucket_pct",
        group=Group.ROUTINES,
        type=_F,
        description="D31: day P&L step (% of equity) that counts as a change for the "
        "no-change digest.",
        target=Target.ROUTINES,
        risk=Risk.UP,
        path=("loop", "pnl_bucket_pct"),
        unit="%",
        min=0.05,
        max=10,
        hard_ceiling=10,
    ),
    Tunable(
        key="loop.post_hold_roots",
        group=Group.ROUTINES,
        type=_B,
        description="D36: post a HOLD root line for skipped loop slots too.",
        target=Target.ROUTINES,
        risk=Risk.NONE,
        path=("loop", "post_hold_roots"),
    ),
    Tunable(
        key="loop.slack_layout",
        group=Group.ROUTINES,
        type=ValueType.CHOICE,
        description="D36: one root line per loop slot in #arc-investor (root_per_loop) or "
        "everything in the day thread (day_thread, the rollback).",
        target=Target.ROUTINES,
        risk=Risk.NONE,
        path=("loop", "slack_layout"),
        choices=("root_per_loop", "day_thread"),
    ),
    # E4.8a (D46/D44): Finnhub facts in the Scalp/Research prompts. Strategy lane:
    # the default stays off until an experiment (XP-2) returns a `win` verdict.
    Tunable(
        key="personas.finnhub_context",
        group=Group.ROUTINES,
        type=ValueType.CHOICE,
        description="E4.8a: show the Finnhub per-ticker facts (earnings surprises, insider, "
        "analyst recs, fundamentals) to the Scalp and Research. Experiment XP-2 tests it.",
        target=Target.ROUTINES,
        risk=Risk.ORDER,
        path=("personas", "finnhub_context"),
        choices=("off", "on"),
        aliases=("routines.personas.finnhub_context", "finnhub_context"),
    ),
    # E14.3 (D60/D44): Alpaca movers + most-actives as a "Tape movers" block in the
    # Scalp prompt. Strategy lane: off until an experiment (XP-11) returns `win`.
    Tunable(
        key="personas.scalp_movers_context",
        group=Group.ROUTINES,
        type=ValueType.CHOICE,
        description="E14.3: show the Scalp a 'Tape movers' block (Alpaca movers + "
        "most-actives, only active-list names and names a story mentions, at most 10 "
        "lines). Context only, never a discovery input. Experiment XP-11 tests it.",
        target=Target.ROUTINES,
        risk=Risk.ORDER,
        path=("personas", "scalp_movers_context"),
        choices=("off", "on"),
        aliases=("routines.personas.scalp_movers_context", "scalp_movers_context"),
    ),
    # E12.5 (D51/D44): Research diversification. Strategy lane: strict is the
    # control; relaxed lets two same-industry names rank and loosens the drops.
    Tunable(
        key="personas.director_diversification",
        group=Group.ROUTINES,
        type=ValueType.CHOICE,
        description="E12.5: how strictly Research diversifies. strict = E5.9 drops; "
        "relaxed = two names per industry may rank, adds_concentration drops only on a "
        "flagged sector once the industry holds director_diversification."
        "max_names_per_industry names, looser flag thresholds. Experiment XP-3 tests it.",
        target=Target.ROUTINES,
        risk=Risk.ORDER,
        path=("personas", "director_diversification"),
        choices=("strict", "relaxed"),
        aliases=("routines.personas.director_diversification", "director_diversification"),
    ),
    # E14.5 (D60/D44): Reddit mention velocity in the Scout's retail-buzz section.
    # Strategy lane (a Scout prompt change): default off.
    Tunable(
        key="personas.scout_buzz_velocity",
        group=Group.ROUTINES,
        type=ValueType.CHOICE,
        description="E14.5: show each Reddit name's code-computed mention velocity "
        "((m + k) / (m24 + k)) and a 'Fastest risers' line in the Scout's retail-buzz "
        "section. Context only; the trending tier is unchanged.",
        target=Target.ROUTINES,
        risk=Risk.ORDER,
        path=("personas", "scout_buzz_velocity"),
        choices=("off", "on"),
        aliases=("routines.personas.scout_buzz_velocity", "scout_buzz_velocity"),
    ),
    # E14.6 (D60/D44): Stocktwits per-ticker sentiment in the Scout and Research
    # prompts. Strategy lane (a prompt change): default off; draft XP-12 tests on.
    Tunable(
        key="personas.retail_sentiment_context",
        group=Group.ROUTINES,
        type=ValueType.CHOICE,
        description="E14.6: add the code-computed Stocktwits bull/bear ratio (user-tagged "
        "messages, min 5 tagged) as a 'Retail sentiment' block in the Scout prompt and one "
        "fact per Research pool line. Context only; never a gate or ranking input.",
        target=Target.ROUTINES,
        risk=Risk.ORDER,
        path=("personas", "retail_sentiment_context"),
        choices=("off", "on"),
        aliases=("routines.personas.retail_sentiment_context", "retail_sentiment_context"),
    ),
)

# E8.2a: ops-alert thresholds under `monitoring:` in routines.yaml. They only shape
# #project-arc alerts (never trading), so most apply immediately; making an alert
# quieter (lower coverage bar, more slow ticks tolerated) needs a confirm.
_MONITORING_TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        key="monitoring.per_slot_min_interval",
        group=Group.ROUTINES,
        type=_I,
        description="E8.2a: jobs whose slots are at least this many minutes apart get one "
        "missed-window alert per slot; faster jobs get one slot-coverage alert per job "
        "(5 = every job per slot, the pre-E8.2a behaviour).",
        target=Target.ROUTINES,
        risk=Risk.NONE,
        path=("monitoring", "per_slot_min_interval"),
        unit="m",
        min=5,
        max=1440,
    ),
    Tunable(
        key="monitoring.coverage_window",
        group=Group.ROUTINES,
        type=_I,
        description="E8.2a: rolling window (minutes) for slot coverage and slow ticks.",
        target=Target.ROUTINES,
        risk=Risk.NONE,
        path=("monitoring", "coverage_window"),
        unit="m",
        min=15,
        max=240,
    ),
    Tunable(
        key="monitoring.coverage_min",
        group=Group.ROUTINES,
        type=_F,
        description="E8.2a: a fast job alerts when it ran fewer than this share of its "
        "slots in the coverage window.",
        target=Target.ROUTINES,
        risk=Risk.DOWN,
        path=("monitoring", "coverage_min"),
        unit="pct",
        min=0.1,
        max=1.0,
        hard_ceiling=0.1,
    ),
    Tunable(
        key="monitoring.tick_slow_count",
        group=Group.ROUTINES,
        type=_I,
        description="E8.2a: slow ticks in the coverage window that open a tick_slow alert.",
        target=Target.ROUTINES,
        risk=Risk.UP,
        path=("monitoring", "tick_slow_count"),
        min=1,
        max=20,
        hard_ceiling=20,
    ),
    Tunable(
        key="monitoring.tick_slow_after",
        group=Group.ROUTINES,
        type=_I,
        description="E8.2a: a tick taking longer than this many minutes counts as slow.",
        target=Target.ROUTINES,
        risk=Risk.UP,
        path=("monitoring", "tick_slow_after"),
        unit="m",
        min=1,
        max=30,
        hard_ceiling=30,
    ),
    Tunable(
        key="monitoring.earnings_stale_after",
        group=Group.ROUTINES,
        type=_I,
        description="E4.1d: coverage:earnings alerts when no earnings-calendar doc was "
        "stored for this many days while the universe has a stock (blackout dates).",
        target=Target.ROUTINES,
        risk=Risk.UP,
        path=("monitoring", "earnings_stale_after"),
        unit="d",
        min=1,
        max=30,
        hard_ceiling=30,
    ),
)

# E8.8b (D48): Arc Tower display knobs under `tower:` in routines.yaml. Display only (the
# tower is read-only and no trading code reads them), so a change applies immediately.
_TOWER_TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        key="tower.overview.activity_hours",
        group=Group.ROUTINES,
        type=_I,
        description="E8.8b: the Overview's Recent Activity shows this many rolling hours.",
        target=Target.ROUTINES,
        risk=Risk.NONE,
        path=("tower", "overview", "activity_hours"),
        unit="h",
        min=1,
        max=168,
    ),
)


def _category_tunables() -> tuple[Tunable, ...]:
    """D47/D49/D56 (E4.7, E4.9, E13.3): each source category's weight and freshness window.

    All six categories have a duration window (D56: options_fast 30m, options_slow
    24h). The D47 key of the renamed ``company`` category is an alias of the new key,
    so a stored override on it applies to ``company_data``
    (:data:`CATEGORY_KEY_RENAMES`). Keys of removed categories are orphaned
    (:data:`ORPHANED_KEY_PREFIXES`).
    """
    from arc.context.categories import SourceCategory

    renamed = {new: old for old, new in CATEGORY_KEY_RENAMES.items()}
    out: list[Tunable] = []
    for c in SourceCategory:
        old = renamed.get(c.value)
        out.append(
            Tunable(
                key=f"categories.{c.value}.weight",
                group=Group.ROUTINES,
                type=_F,
                description=f"D49: {c.value} share relative to the other categories (all 1 = "
                "equal; 0 = never read). Sources (or YouTube channels) split it.",
                target=Target.ROUTINES,
                risk=Risk.ANY,
                path=("categories", c.value, "weight"),
                min=0,
                max=5,
                hard_ceiling=5,
                aliases=(f"categories.{old}.weight",) if old else (),
            )
        )
        out.append(
            Tunable(
                key=f"categories.{c.value}.max_age",
                group=Group.ROUTINES,
                type=_I,
                description=f"D49: {c.value} freshness window (minutes). Older docs are "
                "never stored or read (skipped_stale); older typed context shows as stale.",
                target=Target.ROUTINES,
                risk=Risk.UP,
                path=("categories", c.value, "max_age"),
                unit="m",
                min=30,
                max=10_080,
                hard_ceiling=10_080,
                aliases=(f"categories.{old}.max_age",) if old else (),
            )
        )
    return tuple(out)


# D56 (E13.3, folds E5.14's config half): the `funnel:` block in routines.yaml. Read
# by the Scout (E13.7), the coverage:scout alert and the Tower funnel report (E13.14).
_FUNNEL_TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        key="funnel.scout.max_discovery",
        group=Group.ROUTINES,
        type=_I,
        description="D56: most names the Scout may admit to the discovery tier per day.",
        target=Target.ROUTINES,
        risk=Risk.UP,
        path=("funnel", "scout", "max_discovery"),
        min=0,
        max=25,
        hard_ceiling=25,
    ),
    Tunable(
        key="funnel.scout.min_discovery_alert",
        group=Group.ROUTINES,
        type=_I,
        description="D56: the coverage:scout ops alert fires when the Scout admits fewer "
        "discovery names than this.",
        target=Target.ROUTINES,
        risk=Risk.NONE,
        path=("funnel", "scout", "min_discovery_alert"),
        min=0,
        max=20,
    ),
    Tunable(
        key="funnel.research.max_scout_only_ideas",
        group=Group.ROUTINES,
        type=_I,
        description="D56: most Scout-only ideas (no Scalp story) Research weighs per loop.",
        target=Target.ROUTINES,
        risk=Risk.UP,
        path=("funnel", "research", "max_scout_only_ideas"),
        min=0,
        max=50,
        hard_ceiling=50,
    ),
)

# E13.5 (D56): the `options_slow:` block in routines.yaml (Cboe daily stats + VX curve).
_OPTIONS_SLOW_TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        key="options_slow.vx_flat_band",
        group=Group.ROUTINES,
        type=_F,
        description="E13.5: the VX futures curve reads 'flat' when the month1 -> month2 "
        "slope is within this many percent (context data only, never a gate input).",
        target=Target.ROUTINES,
        risk=Risk.NONE,
        path=("options_slow", "vx_flat_band"),
        min=0.0,
        max=10.0,
    ),
)

# E13.6 (D56): source-job options that are runtime-tunable (plain YAML paths under a
# `sources:` job; read and written as is, not as a cadence).
_SOURCE_OPTION_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("sources", "options_fast", "max_tickers"),
        ("sources", "universe.trending", "scoring"),  # E14.5
    }
)
_OPTIONS_FAST_TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        key="sources.options_fast.max_tickers",
        group=Group.ROUTINES,
        type=_I,
        description="E13.6: most tickers (active list, then open underlyings) whose Cboe "
        "delayed chain the 30-min options tape snapshots (one request each).",
        target=Target.ROUTINES,
        risk=Risk.NONE,
        path=("sources", "options_fast", "max_tickers"),
        min=1,
        max=60,
        hard_ceiling=60,
    ),
    # E14.5 (D60/D44): the trending ranker's Reddit scoring. Strategy lane (a selection
    # change): rank_gain is the control; draft XP-10 tests velocity.
    Tunable(
        key="universe.trending.scoring",
        group=Group.ROUTINES,
        type=ValueType.CHOICE,
        description="E14.5: how the trending tier scores Reddit: mentions plus the 24 h "
        "rank gain (rank_gain) or plus the mention velocity (m + k) / (m24 + k) (velocity). "
        "Experiment XP-10 tests velocity.",
        target=Target.ROUTINES,
        risk=Risk.ORDER,
        path=("sources", "universe.trending", "scoring"),
        choices=("rank_gain", "velocity"),
        aliases=(
            "routines.universe.trending.scoring",
            "sources.universe.trending.scoring",
            "routines.sources.universe.trending.scoring",
        ),
    ),
)


# D64 (E14.7): the `universe.carryover` block in routines.yaml (discovery + trending
# two-run merge). On by default (owner decision, not an experiment).
_CARRYOVER_TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        key="universe.carryover.enabled",
        group=Group.UNIVERSE,
        type=_B,
        description="D64: discovery + trending entries merge the previous run's names "
        "(48 h, 0.6 x today + 0.4 x previous). Off = each run's list only.",
        target=Target.ROUTINES,
        risk=Risk.TRUE,
        path=("universe", "carryover", "enabled"),
        aliases=("routines.universe.carryover.enabled", "carryover.enabled"),
    ),
    Tunable(
        key="universe.carryover.window_h",
        group=Group.UNIVERSE,
        type=_I,
        description="D64: the previous run is merged only when written this many hours "
        "before the new run (strict clock hours; Monday's run has no previous run at 48).",
        target=Target.ROUTINES,
        risk=Risk.UP,
        unit="h",
        path=("universe", "carryover", "window_h"),
        min=24,
        max=96,
        hard_ceiling=96,
        aliases=("routines.universe.carryover.window_h", "carryover.window_h"),
    ),
    Tunable(
        key="universe.carryover.w_today",
        group=Group.UNIVERSE,
        type=_F,
        description="D64: weight of this run's score in the combined score (the previous "
        "run gets 1 - w_today).",
        target=Target.ROUTINES,
        risk=Risk.DOWN,
        path=("universe", "carryover", "w_today"),
        min=0.5,
        max=1.0,
        hard_ceiling=0.5,
        aliases=("routines.universe.carryover.w_today", "carryover.w_today"),
    ),
)


# D49: D47 category names renamed in place (old -> new). Their tunable keys stay as
# aliases, so a change-log override on ``categories.company.weight`` applies to
# ``categories.company_data.weight``. ``video`` was split, so its keys have no single
# successor: an override on them is reported and dropped (control.override_unknown_key).
CATEGORY_KEY_RENAMES: dict[str, str] = {"company": "company_data"}

# D56 (E13.3): keys of removed categories and the removed UOA detector. A stored
# override on one has no successor: it is logged as ``config.override_orphaned`` and
# ignored (``macro_data`` / ``macro`` split into market_news + reference data;
# ``options_data`` became options_fast + options_slow; ``uoa_*`` left with the kind).
ORPHANED_KEY_PREFIXES: tuple[str, ...] = (
    "categories.macro_data.",
    "categories.macro.",
    "categories.options_data.",
    "uoa_",
    "settings.uoa_",
    "universe_screen_relaxed_",  # E13.15: D51 relaxed screen
)
# E13.15: exact keys of removed D51 / flag tunables (a prefix would also match
# ``universe_momentum_size_d56``).
ORPHANED_KEYS: frozenset[str] = frozenset(
    {
        "universe.tiers.model",
        "universe_momentum_size",
        "scalp_max_new_tickers",
        "sweep_max_new_tickers",
        "scout_max_new_tickers",
        # the D56 cutover switches, always on now (arc.routines.config.REMOVED_PERSONA_SWITCHES)
        "personas.quant_risk_loop",
        "personas.scalp_options_tape",
        "personas.scout_feed",
        "personas.research_idea_pool",
        "personas.research_compact_prompt",
        "personas.exit_path",
        # D57 (E3.5): the share-count delta cap, replaced by portfolio_dollar_delta_cap_pct
        # (no alias: a multiple of equity/100 does not convert to a share of equity)
        "portfolio_delta_cap",
    }
)


def is_orphaned(key: str) -> bool:
    """D56: *key* belongs to a removed category, the removed UOA detector or a removed
    D51 / flag tunable (E13.15), or the D57 share-count delta cap (E3.5)."""
    return key in ORPHANED_KEYS or key.startswith(ORPHANED_KEY_PREFIXES)


def _exp(key: str, desc: str, risk: Risk, path: tuple[str, ...], **kw: Any) -> Tunable:
    """A ``config/experiments.yaml`` default (E10.1, D44): copied into new specs only."""
    return Tunable(
        key=f"experiments.{key}",
        group=Group.EXPERIMENTS,
        type=kw.pop("type", _F),
        description=f"D44 forward experiments: {desc} Default for new specs; a registered "
        "(hash-locked) experiment keeps the value it was registered with.",
        target=Target.EXPERIMENTS,
        risk=risk,
        path=("experiments", "defaults", *path),
        **kw,
    )


# E10.1 (D44): forward A/B experiment defaults. Riskier = a verdict on less evidence
# (higher alpha, lower power, fewer sessions).
_EXPERIMENT_TUNABLES: tuple[Tunable, ...] = (
    _exp(
        "alpha",
        "two-sided significance level of the always-valid (mSPRT) CI.",
        Risk.UP,
        ("alpha",),
        min=0.001,
        max=0.2,
        hard_ceiling=0.2,
    ),
    _exp(
        "power",
        "power the MDE is sized for.",
        Risk.DOWN,
        ("power",),
        min=0.5,
        max=0.99,
        hard_ceiling=0.5,
    ),
    _exp(
        "min_sessions",
        "no win/futility verdict before this many paired sessions.",
        Risk.DOWN,
        ("min_sessions",),
        type=_I,
        min=5,
        max=250,
        hard_ceiling=5,
    ),
    _exp(
        "max_sessions",
        "futility stop after this many paired sessions.",
        Risk.ANY,
        ("max_sessions",),
        type=_I,
        min=5,
        max=250,
    ),
    _exp(
        "aa_sessions",
        "length of an A/A run (sessions) that measures sigma and the MDE.",
        Risk.DOWN,
        ("aa_sessions",),
        type=_I,
        min=5,
        max=60,
        hard_ceiling=5,
    ),
    # E10.3: how the daily evaluation computes its numbers (not copied into specs).
    Tunable(
        key="experiments.stats.sigma_upper_q",
        group=Group.EXPERIMENTS,
        type=_F,
        description="D44 evaluation: with no A/A sigma on record, the running sd of the daily "
        "difference is inflated to its (1 - q) upper chi-square bound (larger q = narrower CI).",
        target=Target.EXPERIMENTS,
        risk=Risk.UP,
        path=("experiments", "stats", "sigma_upper_q"),
        min=0.01,
        max=0.25,
        hard_ceiling=0.25,
    ),
    Tunable(
        key="experiments.stats.bootstrap_resamples",
        group=Group.EXPERIMENTS,
        type=_I,
        description="D44 evaluation: paired bootstrap resamples for the Sortino "
        "non-inferiority CI (seeded, deterministic).",
        target=Target.EXPERIMENTS,
        risk=Risk.DOWN,
        path=("experiments", "stats", "bootstrap_resamples"),
        min=200,
        max=20000,
        hard_ceiling=200,
    ),
)


def _runner(key: str, desc: str, risk: Risk, **kw: Any) -> Tunable:
    """``experiments.runner.*`` (E10.2, D44): how arms pair with control's loop.

    The arm list itself (keys, store, spec arm), ``arm_jobs`` and ``arm_personas``
    (E13.12: which personas an arm runs itself) are experiment topology, never
    tunable: an arm's broker keys, store or persona set must not change from Slack.
    """
    return Tunable(
        key=f"experiments.runner.{key}",
        group=Group.EXPERIMENTS,
        type=kw.pop("type", _I),
        description=f"D44 experiment arm runner: {desc}",
        target=Target.EXPERIMENTS,
        risk=risk,
        path=("experiments", "runner", key),
        **kw,
    )


_EXPERIMENT_TUNABLES += (
    _runner(
        "enabled",
        "run the configured arms paired with control's trading loop while an experiment "
        "is running (off = the arms stop trading; control is unaffected).",
        Risk.TRUE,
        type=_B,
    ),
    _runner(
        "max_lag_seconds",
        "skip pairing a control loop chain older than this (its market inputs are stale).",
        Risk.UP,
        unit="s",
        min=30,
        max=1800,
        hard_ceiling=1800,
    ),
    _runner(
        "tape_keep_days",
        "days of the control loop's recorded market reads kept for pairing and audit.",
        Risk.NONE,
        min=1,
        max=30,
    ),
)


REGISTRY: dict[str, Tunable] = {
    t.key: t
    for t in (
        *_STATIC,
        *_exit_tunables(),
        *_LOOP_TUNABLES,
        *_MONITORING_TUNABLES,
        *_TOWER_TUNABLES,
        *_category_tunables(),
        *_FUNNEL_TUNABLES,
        *_OPTIONS_SLOW_TUNABLES,
        *_OPTIONS_FAST_TUNABLES,
        *_CARRYOVER_TUNABLES,
        *_EXPERIMENT_TUNABLES,
    )
}
_ALIASES: dict[str, str] = {a: t.key for t in REGISTRY.values() for a in t.aliases}


def _pattern_tunable(key: str) -> Tunable | None:
    """Keys whose name carries a routine job or a profile (validated by the caller)."""
    m = _ROUTINE_KEY_RE.match(key)
    if m:
        job, attr = m.group("job"), m.group("attr")
        if attr == "enabled":
            return Tunable(
                key=key,
                group=Group.ROUTINES,
                type=_B,
                description=f"Routine {job}: enabled.",
                target=Target.ROUTINES,
                risk=Risk.ANY,
                path=(job, "enabled"),
            )
        return Tunable(
            key=key,
            group=Group.ROUTINES,
            type=ValueType.CADENCE,
            description=f"Routine {job}: cadence ('every 30m 09:30-16:00' or 'at 09:30,12:00').",
            target=Target.ROUTINES,
            risk=Risk.ANY,
            path=(job,),
        )
    m = _PROFILE_KEY_RE.match(key)
    if m:
        name, attr = m.group("name"), m.group("attr")
        return Tunable(
            key=key,
            group=Group.ENTRIES,
            type=_I,
            description=f"Account profile {name}: entry DTE window, {attr[4:]} bound.",
            target=Target.PROFILES,
            risk=Risk.DOWN if attr == "dte_min" else Risk.NONE,
            path=("profiles", name, attr),
            min=7,
            max=120,
            hard_ceiling=7 if attr == "dte_min" else None,
        )
    return None


def lookup(key: str) -> Tunable:
    """The registry entry for *key* (or an alias); raises :class:`TunableError`."""
    k = key.strip()
    lowered = k.lower()
    if (
        lowered in NEVER_TUNABLE
        or lowered in NEVER_TUNABLE_PATHS
        or lowered.startswith(("arc_", "gate", "secret"))
    ):
        msg = f"{k!r} is never tunable (ARC_ENV, gate code and secrets are fixed; D26)"
        raise TunableError(msg)
    k = _ALIASES.get(lowered, lowered)
    if k in REGISTRY:
        return REGISTRY[k]
    pat = _pattern_tunable(k)
    if pat is not None:
        return pat
    msg = f"unknown key {key!r}; see `!arc config` for the tunable keys"
    raise TunableError(msg)


def is_alias(key: str) -> bool:
    """True for an alias of a registry key (e.g. a D49-renamed ``categories.company.*``)."""
    lowered = key.strip().lower()
    return lowered in _ALIASES


def keys_in_group(group: Group) -> list[Tunable]:
    return [t for t in REGISTRY.values() if t.group is group]


# ---------------------------------------------------------------------------
# Parsing + validation
# ---------------------------------------------------------------------------

_TRUE = {"on", "true", "yes", "1", "enable", "enabled"}
_FALSE = {"off", "false", "no", "0", "disable", "disabled"}


def _num(t: Tunable, raw: str) -> float:
    text = raw.strip().replace(",", "").replace("$", "")
    pct = text.endswith("%")
    if pct:
        text = text[:-1]
    if text.endswith(t.unit) and t.unit in {"s", "d", "h"}:
        text = text[: -len(t.unit)]
    try:
        v = float(text)
    except ValueError:
        msg = f"{t.key}: {raw!r} is not a number"
        raise TunableError(msg) from None
    if not math.isfinite(v):
        msg = f"{t.key}: {raw!r} is not a finite number"
        raise TunableError(msg)
    if pct:
        if t.unit != "pct":
            msg = f"{t.key}: '%' only applies to percentage keys"
            raise TunableError(msg)
        v /= 100.0
    return v


def _check_bounds(t: Tunable, v: float) -> None:
    if t.hard_ceiling is not None:
        over = v > t.hard_ceiling if t.risk is Risk.UP else v < t.hard_ceiling
        if t.risk in (Risk.UP, Risk.DOWN) and over:
            side = "above" if t.risk is Risk.UP else "below"
            msg = (
                f"{t.key}: {format_value(t, v)} is {side} the hard ceiling "
                f"{format_value(t, t.hard_ceiling)} (a code constant; changing it needs a PR)"
            )
            raise TunableError(msg)
    if (t.min is not None and v < t.min) or (t.max is not None and v > t.max):
        msg = f"{t.key}: {format_value(t, v)} is outside {t.bounds}"
        raise TunableError(msg)


def _list_edit(t: Tunable, raw: str, current: list[str], norm: Any) -> list[str]:
    parts = [p.strip() for p in re.split(r"[,\s]+", raw.strip()) if p.strip()]
    if not parts:
        msg = f"{t.key}: empty value"
        raise TunableError(msg)
    edits = all(p[0] in "+-" for p in parts)
    if any(p[0] in "+-" for p in parts) and not edits:
        msg = f"{t.key}: mix of edits (+X/-X) and plain values; use one form"
        raise TunableError(msg)
    if edits:
        out = list(current)
        for p in parts:
            item = norm(p[1:])
            if p[0] == "+" and item not in out:
                out.append(item)
            elif p[0] == "-":
                if item not in out:
                    msg = f"{t.key}: {item} is not in the current list"
                    raise TunableError(msg)
                out.remove(item)
    else:
        out = []
        for p in parts:
            item = norm(p)
            if item not in out:
                out.append(item)
    if not out:
        msg = f"{t.key}: the list may not be empty"
        raise TunableError(msg)
    if t.max_items is not None and len(out) > t.max_items:
        msg = f"{t.key}: {len(out)} items > hard ceiling {t.max_items}"
        raise TunableError(msg)
    return out


def _ticker(s: str) -> str:
    sym = s.strip().upper()
    if not _TICKER_RE.match(sym):
        msg = f"universe: {s!r} is not a ticker symbol"
        raise TunableError(msg)
    return sym


def _user(s: str) -> str:
    uid = s.strip().strip("<@>").upper()
    if not _USER_RE.match(uid):
        msg = f"approver_ids: {s!r} is not a Slack user id"
        raise TunableError(msg)
    return uid


_EVERY_RE = re.compile(r"^every\s+(\d+[smhd])(?:\s+(\d{2}:\d{2}-\d{2}:\d{2}))?$")
_AT_RE = re.compile(r"^at\s+(\d{2}:\d{2}(?:\s*,\s*\d{2}:\d{2})*)$")


def _cadence(t: Tunable, raw: str) -> str:
    text = " ".join(raw.strip().lower().split())
    m = _EVERY_RE.match(text)
    if m:
        return f"every {m.group(1)}" + (f" {m.group(2)}" if m.group(2) else "")
    m = _AT_RE.match(text)
    if m:
        times = [x.strip() for x in m.group(1).split(",")]
        for hhmm in times:
            h, mi = (int(x) for x in hhmm.split(":"))
            if h > 23 or mi > 59:
                msg = f"{t.key}: invalid time {hhmm!r}"
                raise TunableError(msg)
        return "at " + ",".join(times)
    msg = f"{t.key}: {raw!r}; use 'every 30m [09:30-16:00]' or 'at 09:30,12:00' (ET)"
    raise TunableError(msg)


def _targets(t: Tunable, raw: str) -> list[dict[str, float | int]]:
    text = raw.strip().lower()
    if text in {"none", "off", "clear", "[]"}:
        return []
    out: list[dict[str, float | int]] = []
    for part in [p for p in re.split(r"[,\s]+", text) if p]:
        dte_s, sep, pct_s = part.partition(":")
        if not sep:
            msg = f"{t.key}: {part!r}; expected '<dte>:<pct>', e.g. 14:0.35 or 14:35%"
            raise TunableError(msg)
        try:
            dte = int(dte_s)
            pct = float(pct_s.rstrip("%")) / (100.0 if pct_s.endswith("%") else 1.0)
        except ValueError:
            msg = f"{t.key}: {part!r} is not '<dte>:<pct>'"
            raise TunableError(msg) from None
        if not (0 <= dte <= 60) or not (0.0 < pct <= 3.0):
            msg = f"{t.key}: {part!r} out of range (dte 0-60, pct 0-300%)"
            raise TunableError(msg)
        out.append({"dte_lte": dte, "take_profit_pct": pct})
    if len({o["dte_lte"] for o in out}) != len(out):
        msg = f"{t.key}: duplicate dte buckets"
        raise TunableError(msg)
    if t.max_items is not None and len(out) > t.max_items:
        msg = f"{t.key}: {len(out)} targets > hard ceiling {t.max_items}"
        raise TunableError(msg)
    return sorted(out, key=lambda o: o["dte_lte"])


def parse_value(
    t: Tunable, raw: str, *, current: Any = None, base_list: list[str] | None = None
) -> Any:
    """Parse and validate *raw* for *t*; the canonical stored value.

    *current* is the effective value (for ``+X``/``-X`` list edits); *base_list*
    is the env/code list a :attr:`ValueType.USER_IDS` value must stay inside.
    """
    typ = t.type
    if typ is ValueType.BOOL:
        v = raw.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        msg = f"{t.key}: {raw!r}; use on/off"
        raise TunableError(msg)
    if typ is ValueType.CHOICE:
        v = raw.strip().lower()
        if v not in t.choices:
            msg = f"{t.key}: {raw!r} is not one of {t.bounds}"
            raise TunableError(msg)
        return v
    if typ is ValueType.INT:
        f = _num(t, raw)
        if f != int(f):
            msg = f"{t.key}: {raw!r} is not a whole number"
            raise TunableError(msg)
        _check_bounds(t, f)
        return int(f)
    if typ is ValueType.FLOAT:
        f = _num(t, raw)
        _check_bounds(t, f)
        return f
    if typ is ValueType.FLOAT_OR_NONE:
        if raw.strip().lower() in {"none", "off", "null"}:
            return None  # no stop: the riskiest side (direction() treats None as +inf)
        f = _num(t, raw)
        _check_bounds(t, f)
        return f
    if typ is ValueType.TICKERS:
        return _list_edit(t, raw, list(current or []), _ticker)
    if typ is ValueType.USER_IDS:
        out = _list_edit(t, raw, list(current or []), _user)
        allowed = set(base_list or [])
        extra = [u for u in out if u not in allowed]
        if extra:
            msg = (
                f"{t.key}: {', '.join(extra)} not in ARC_APPROVER_SLACK_USER_IDS; Slack can only "
                "narrow the approver list (hard ceiling)"
            )
            raise TunableError(msg)
        return out
    if typ is ValueType.CADENCE:
        return _cadence(t, raw)
    if typ is ValueType.TARGETS:
        return _targets(t, raw)
    raise TunableError(f"{t.key}: unsupported type {typ}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Risk direction
# ---------------------------------------------------------------------------


def _as_num(v: Any) -> float:
    return math.inf if v is None else float(v)


def direction(t: Tunable, old: Any, new: Any) -> Direction:
    """Is ``old -> new`` safer, riskier or neutral for *t*?"""
    if old == new:
        return Direction.UNCHANGED
    r = t.risk
    if r is Risk.NONE:
        return Direction.NEUTRAL
    if r is Risk.ANY:
        return Direction.RISKIER
    if r in (Risk.UP, Risk.DOWN):
        a, b = _as_num(old), _as_num(new)
        up = b > a
        riskier = up if r is Risk.UP else not up
        return Direction.RISKIER if riskier else Direction.SAFER
    if r is Risk.ORDER:
        order = list(t.choices)
        try:
            up = order.index(str(new)) > order.index(str(old))
        except ValueError:
            return Direction.RISKIER  # unknown old value: be conservative
        return Direction.RISKIER if up else Direction.SAFER
    if r is Risk.TRUE:
        return Direction.RISKIER if bool(new) else Direction.SAFER
    if r is Risk.FALSE:
        return Direction.SAFER if bool(new) else Direction.RISKIER
    if r is Risk.GROW:
        added = set(new or []) - set(old or [])
        return Direction.RISKIER if added else Direction.SAFER
    return Direction.RISKIER  # pragma: no cover - every Risk is handled above


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def format_value(t: Tunable, v: Any) -> str:
    """Owner-facing text for a value of *t* (``5%``, ``on``, ``SPY, QQQ``, ``none``)."""
    if v is None:
        return "none"
    if t.type is ValueType.BOOL or isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, list):
        if not v:
            return "none"
        if isinstance(v[0], dict):
            return ", ".join(f"{d['dte_lte']}d:{float(d['take_profit_pct']):.0%}" for d in v)
        return ", ".join(str(x) for x in v)
    if isinstance(v, float | int) and t.unit == "pct":
        return f"{float(v) * 100:.4g}%"
    if isinstance(v, float) and t.unit == "$":
        return f"${v:,.2f}"
    if isinstance(v, int | float) and t.unit in {"s", "d", "h"}:
        return f"{v:g}{t.unit}"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


# ---------------------------------------------------------------------------
# YAML targets: read the current value from, and write a value into, raw file data
# ---------------------------------------------------------------------------

DEFAULT_STOP_VALUE = 0.75  # D23 relaxed stop, used when a stop is created from 'none'
_SECTIONS = ("sources", "personas")
# Top-level routines.yaml sections whose tunables are plain paths (not per job).
_PLAIN_ROUTINE_SECTIONS = (
    ("loop",),
    ("monitoring",),
    ("categories",),
    ("tower",),
    ("funnel",),
    ("options_slow",),  # E13.5
    ("universe",),  # D64 (E14.7): universe.carryover.*
)
# Scalar switches that sit next to the jobs under `personas:` (E4.8a), as `on | off`.
_PERSONA_SWITCHES = frozenset(
    {
        ("personas", "finnhub_context"),
        ("personas", "scout_buzz_velocity"),  # E14.5
        ("personas", "scalp_movers_context"),  # E14.3
        ("personas", "retail_sentiment_context"),  # E14.6
    }
)
# Scalar choice switches under `personas:` (E12.5) -> the control value when absent.
_PERSONA_CHOICE_SWITCHES: dict[tuple[str, ...], str] = {
    ("personas", "director_diversification"): "strict",
}


def _get(data: Any, path: tuple[str, ...]) -> Any:
    node = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _routine(t: Tunable, raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    job = t.path[0]
    for section in _SECTIONS:
        spec = (raw.get(section) or {}).get(job)
        if isinstance(spec, dict):
            return section, spec
    msg = f"{t.key}: no source or persona job {job!r} in routines.yaml"
    raise TunableError(msg)


def _cadence_text(spec: dict[str, Any]) -> str:
    if spec.get("trigger"):
        return f"on {spec['trigger']}"
    if spec.get("every"):
        window = f" {spec['window']}" if spec.get("window") else ""
        return f"every {spec['every']}{window}"
    sched = spec.get("schedule") or []
    sched = [sched] if isinstance(sched, str) else sched
    return "at " + ",".join(str(s) for s in sched)


def _minutes(text: str) -> int:
    """``"30m"`` / ``"2h"`` / ``"90s"`` → whole minutes (the loop's duration strings)."""
    t = text.strip().lower()
    n, unit = float(t[:-1]), t[-1]
    secs = n * {"s": 1, "m": 60, "h": 3600}[unit]
    return int(round(secs / 60))


def read_raw(t: Tunable, raw: dict[str, Any]) -> Any:
    """The value of YAML-targeted *t* in *raw* file data, in registry form."""
    if t.target is Target.ROUTINES and t.path in _PERSONA_SWITCHES:
        v = _get(raw, t.path)
        if v is None:
            return "off"  # an absent switch is off (the control behaviour)
        if isinstance(v, bool):  # YAML 1.1 reads a bare on/off as a boolean
            return "on" if v else "off"
        return str(v).strip().lower()
    if t.target is Target.ROUTINES and t.path in _PERSONA_CHOICE_SWITCHES:
        v = _get(raw, t.path)
        return _PERSONA_CHOICE_SWITCHES[t.path] if v is None else str(v).strip().lower()
    if t.target is Target.ROUTINES and t.path[:1] in _PLAIN_ROUTINE_SECTIONS:
        v = _get(raw, t.path)
        if v is None:
            return None
        if t.unit == "m":
            if t.path[:1] == ("categories",):  # D47 Ttl text ("6h", "1 session")
                from arc.context.ttl import Ttl

                d = Ttl.model_validate(v).duration
                return None if d is None else int(round(d.total_seconds() / 60))
            return _minutes(str(v))
        if t.unit == "d" and isinstance(v, str):  # a monitoring duration ("7d")
            return int(round(parse_duration(v).total_seconds() / 86_400))
        return v
    if t.target is Target.ROUTINES and t.path in _SOURCE_OPTION_PATHS:  # E13.6
        return _get(raw, t.path)
    if t.target is Target.ROUTINES:
        _, spec = _routine(t, raw)
        if t.type is ValueType.BOOL:
            return bool(spec.get("enabled", True))
        return _cadence_text(spec)
    if t.path == ("positions", "remaining_ev_floor_eod_only"):
        v = _get(raw, t.path)
        return "eod" if (True if v is None else bool(v)) else "intraday"
    if t.target is Target.EXITS and t.path[:1] == ("kinds",):
        kind_data = _get(raw, t.path[:2])
        rel = t.path[2:]
        if kind_data is None:
            kind_data = raw.get("default") or {}
        if rel == ("stop", "value") or rel == ("stop", "basis"):
            stop = kind_data.get("stop")
            return None if stop is None else stop.get(rel[1])
        v = kind_data.get(rel[0])
        if rel == ("stop_eod_only",):
            return "eod" if (True if v is None else bool(v)) else "intraday"
        if rel == ("time_adjusted_targets",):
            return list(v or [])
        return v
    return _get(raw, t.path)


def write_raw(t: Tunable, value: Any, raw: dict[str, Any]) -> list[tuple[tuple[str, ...], Any]]:
    """``(path, value)`` pairs that set *t* to *value* in *raw* (not modified).

    Pure helper for :mod:`arc.control.effective`; the pairs are applied in order.
    """
    if t.target is Target.ROUTINES and (
        t.path in _PERSONA_SWITCHES or t.path in _PERSONA_CHOICE_SWITCHES
    ):
        return [(t.path, str(value))]
    if t.target is Target.ROUTINES and t.path[:1] in _PLAIN_ROUTINE_SECTIONS:
        if t.unit == "m":
            return [(t.path, f"{int(value)}m")]
        if t.unit == "d":
            return [(t.path, f"{int(value)}d")]
        return [(t.path, value)]
    if t.target is Target.ROUTINES and t.path in _SOURCE_OPTION_PATHS:  # E13.6
        return [(t.path, value)]
    if t.target is Target.ROUTINES:
        section, spec = _routine(t, raw)
        base = (section, t.path[0])
        if t.type is ValueType.BOOL:
            return [((*base, "enabled"), bool(value))]
        if spec.get("trigger"):
            msg = f"{t.key}: a triggered job has no cadence to change"
            raise TunableError(msg)
        text = str(value)
        if text.startswith("every "):
            parts = text.split()
            window = parts[2] if len(parts) > 2 else None  # noqa: PLR2004
            return [
                ((*base, "schedule"), []),
                ((*base, "every"), parts[1]),
                ((*base, "window"), window),
            ]
        times = text[len("at ") :].split(",")
        return [((*base, "schedule"), times), ((*base, "every"), None), ((*base, "window"), None)]
    if t.path == ("positions", "remaining_ev_floor_eod_only"):
        return [(t.path, value == "eod")]
    if t.target is Target.EXITS and t.path[:1] == ("kinds",):
        rel = t.path[2:]
        kind_path = t.path[:2]
        if rel in (("stop", "value"), ("stop", "basis")):
            stop = _get(raw, (*kind_path, "stop"))
            if rel[1] == "value" and value is None:
                return [((*kind_path, "stop"), None)]
            if stop is None:
                credit = t.path[1] in _CREDIT_EXIT_KINDS
                new_stop = {
                    "basis": "pct_max_loss" if credit else "pct_debit",
                    "value": DEFAULT_STOP_VALUE,
                    rel[1]: value,
                }
                return [((*kind_path, "stop"), new_stop)]
            return [(t.path, value)]
        if rel == ("stop_eod_only",):
            return [(t.path, value == "eod")]
        return [(t.path, value)]
    return [(t.path, value)]
