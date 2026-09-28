"""Exit policy + managed-exit EV/PoP model (PLAN D19, D23; card E2.4).

Public API:
    - Policy: :class:`ExitPolicy`, :class:`ExitConfig`, :func:`load_exit_config`
      (``config/exits.yaml``), :func:`resolve_rules`, :func:`check_rules`
    - Model: :func:`model_exits` → :class:`ExitModelResult` (static vs managed)
    - Live positions: :func:`evaluate_position` → :class:`PositionExitState`

E6.2 / E6.4 / E7.2 read their exit rules from here and must not define their own.
"""

from arc.exits.model import (
    MODEL_NAME,
    ExitModelResult,
    ExitSummary,
    ManagedStats,
    StaticStats,
    TriggerLevels,
    analytic_pop,
    model_exits,
)
from arc.exits.policy import (
    DEFAULT_EXITS_PATH,
    HOLD_TO_EXPIRY,
    ExitConfig,
    ExitModelConfig,
    ExitPolicy,
    ExitReason,
    IvModel,
    PipelineExitConfig,
    ResolvedRules,
    StopBasis,
    StopRule,
    TimeAdjustedTarget,
    check_rules,
    load_exit_config,
    resolve_rules,
)
from arc.exits.position import OpenPosition, PositionExitState, PositionMarks, evaluate_position

__all__ = [
    "DEFAULT_EXITS_PATH",
    "HOLD_TO_EXPIRY",
    "MODEL_NAME",
    "ExitConfig",
    "ExitModelConfig",
    "ExitModelResult",
    "ExitSummary",
    "ExitPolicy",
    "ExitReason",
    "IvModel",
    "ManagedStats",
    "OpenPosition",
    "PipelineExitConfig",
    "PositionExitState",
    "PositionMarks",
    "ResolvedRules",
    "StaticStats",
    "StopBasis",
    "StopRule",
    "TimeAdjustedTarget",
    "TriggerLevels",
    "analytic_pop",
    "check_rules",
    "evaluate_position",
    "load_exit_config",
    "model_exits",
    "resolve_rules",
]
