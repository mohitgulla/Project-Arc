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

__all__ = [
    "EXIT_KINDS",
    "NEVER_TUNABLE",
    "NOT_EXPOSED",
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


class Target(StrEnum):
    """Where the effective value lives."""

    SETTINGS = "settings"  # an ArcSettings field
    EXITS = "exits"  # config/exits.yaml
    COSTS = "costs"  # config/costs.yaml
    PROFILES = "account_profiles"  # config/account_profiles.yaml
    ROUTINES = "routines"  # config/routines.yaml


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
        "owner_slack_user_id",
        "config_version",
        "yaml_overrides",
        "scout_hermes_bin",
        "ffmpeg_bin",
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
    "limit_tick": "exchange tick size",
    "execution_poll_seconds": "broker polling plumbing",
    "execution_cancel_confirm_seconds": "broker cancel plumbing",
    "alpaca_data_feed": "data subscription tier",
    "alpaca_options_feed": "data subscription tier",
    "scanner_target_delta": "target inside the tunable short band",
    "scanner_long_target_delta": "target inside the tunable long band",
    "scanner_debit_short_target_delta": "target inside the tunable debit-short band",
    "scanner_risk_free_rate": "pricing input",
    "scanner_iv_lookback": "IV-rank statistics window",
    "scanner_iv_min_obs": "IV-rank statistics guard",
    "scanner_iv_history_dir": "path",
    "ingest_rss_feeds": "sources live in routines.yaml",
    "ingest_rss_timeout_seconds": "network plumbing",
    "ingest_youtube_channels": "sources live in routines.yaml",
    "yt_caption_grace_minutes": "ingestion plumbing",
    "yt_max_audio_minutes": "ingestion plumbing",
    "yt_max_audio_per_run": "ingestion plumbing",
    "yt_caption_sleep_seconds": "ingestion plumbing",
    "yt_caption_cooldown_base_minutes": "ingestion plumbing",
    "yt_caption_cooldown_max_minutes": "ingestion plumbing",
    "yt_caption_cooldown_jitter": "ingestion plumbing",
    "whisper_model": "ingestion model",
    "scout_timeout_seconds": "LLM plumbing",
    "scout_batch_size": "LLM plumbing",
    "scout_max_doc_chars": "LLM plumbing",
    "persona_timeout_seconds": "LLM plumbing",
    "pipeline_max_context_notes": "LLM context size",
}

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

MAX_UNIVERSE = 60  # hard ceiling on the number of underlyings
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_USER_RE = re.compile(r"^[UW][A-Z0-9]{6,20}$")
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
        "Underlyings the Scout/Director may trade. '+NVDA,-TSLA' edits the list; added "
        "tickers must be optionable.",
        Risk.GROW,
        max_items=MAX_UNIVERSE,
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
        "portfolio_delta_cap",
        Group.RISK,
        _F,
        "|net delta| cap as a multiple of equity/100 (gate rule greek_caps).",
        Risk.UP,
        min=0.05,
        max=0.60,
        hard_ceiling=0.60,
    ),
    _s(
        "portfolio_vega_cap_pct",
        Group.RISK,
        _F,
        "|net vega| cap per vol point as a share of equity (gate rule greek_caps).",
        Risk.UP,
        unit="pct",
        min=0.001,
        max=0.01,
        hard_ceiling=0.01,
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
        "scout_min_confidence",
        Group.ENTRIES,
        _F,
        "Scout keeps a candidate only at or above this confidence (lower = more ideas).",
        Risk.DOWN,
        min=0.30,
        max=0.95,
        hard_ceiling=0.30,
    ),
    _s(
        "max_shortlist",
        Group.ENTRIES,
        _I,
        "Tickers the Director may shortlist per run.",
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
        "order_budget.restrictive.director_max_shortlist",
        Group.EXECUTION,
        _I,
        "Restrictive tier: Director shortlist cap.",
        Risk.UP,
        field="order_budget_restrictive_director_max_shortlist",
        min=0,
        max=10,
        hard_ceiling=10,
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
    _s(
        "auto_exit_defined_risk",
        Group.APPROVALS,
        _B,
        "D24: fired exits on defined-risk positions skip the Slack approval (gate still "
        "applies). Off = every exit is a proposal needing an approval.",
        Risk.TRUE,
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


REGISTRY: dict[str, Tunable] = {t.key: t for t in (*_STATIC, *_exit_tunables())}
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
    if lowered in NEVER_TUNABLE or lowered.startswith(("arc_", "gate", "secret")):
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
    if text.endswith(t.unit) and t.unit in {"s", "d"}:
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
    if isinstance(v, int | float) and t.unit in {"s", "d"}:
        return f"{v:g}{t.unit}"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


# ---------------------------------------------------------------------------
# YAML targets: read the current value from, and write a value into, raw file data
# ---------------------------------------------------------------------------

DEFAULT_STOP_VALUE = 0.75  # D23 relaxed stop, used when a stop is created from 'none'
_SECTIONS = ("sources", "personas")


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


def read_raw(t: Tunable, raw: dict[str, Any]) -> Any:
    """The value of YAML-targeted *t* in *raw* file data, in registry form."""
    if t.target is Target.ROUTINES:
        _, spec = _routine(t, raw)
        if t.type is ValueType.BOOL:
            return bool(spec.get("enabled", True))
        return _cadence_text(spec)
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
