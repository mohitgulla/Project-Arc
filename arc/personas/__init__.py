"""JSON schemas, prompt builders, and persona skill definitions.

Public API:
    schemas  — pydantic output models for each persona
    builders — pure-function prompt builders
"""

from arc.personas.builders import (
    AuditorInput,
    DirectorInput,
    ExecutionInput,
    QuantInput,
    RiskInput,
    ScoutInput,
    build_auditor_prompt,
    build_director_prompt,
    build_execution_prompt,
    build_quant_prompt,
    build_risk_prompt,
    build_scout_prompt,
)
from arc.personas.schemas import (
    AnomalyReport,
    AuditorOutput,
    DirectorOutput,
    DirectorRankedItem,
    ExecutionOutput,
    ExecutionPlan,
    ImprovementStep,
    LessonLearned,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantStructureOut,
    RiskAssessment,
    RiskOutput,
    ScoutCandidateOut,
    ScoutOutput,
)

__all__ = [
    # Schemas
    "AnomalyReport",
    "AuditorOutput",
    "DirectorOutput",
    "DirectorRankedItem",
    "ExecutionOutput",
    "ExecutionPlan",
    "ImprovementStep",
    "LessonLearned",
    "QuantGreeks",
    "QuantLeg",
    "QuantOutput",
    "QuantStructureOut",
    "RiskAssessment",
    "RiskOutput",
    "ScoutCandidateOut",
    "ScoutOutput",
    # Builders
    "build_scout_prompt",
    "build_director_prompt",
    "build_quant_prompt",
    "build_risk_prompt",
    "build_execution_prompt",
    "build_auditor_prompt",
    # Input types
    "ScoutInput",
    "DirectorInput",
    "QuantInput",
    "RiskInput",
    "ExecutionInput",
    "AuditorInput",
]
