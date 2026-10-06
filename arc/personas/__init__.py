"""JSON schemas, prompt builders, and persona skill definitions.

Public API:
    schemas  — pydantic output models for each persona
    builders — pure-function prompt builders
"""

from arc.personas.builders import (
    QuantInput,
    ResearchInput,
    RiskInput,
    ScalpInput,
    build_quant_prompt,
    build_research_prompt,
    build_risk_prompt,
    build_scalp_prompt,
)
from arc.personas.schemas import (
    AnomalyReport,
    BrokerPlan,
    ImprovementStep,
    LessonLearned,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantStructureOut,
    ReconcileOutput,
    ResearchOutput,
    ResearchRankedItem,
    RiskAssessment,
    RiskOutput,
    ScalpCandidateOut,
    ScalpOutput,
)

__all__ = [
    # Schemas
    "AnomalyReport",
    "BrokerPlan",
    "ResearchOutput",
    "ResearchRankedItem",
    "ImprovementStep",
    "LessonLearned",
    "QuantGreeks",
    "QuantLeg",
    "QuantOutput",
    "QuantStructureOut",
    "ReconcileOutput",
    "RiskAssessment",
    "RiskOutput",
    "ScalpCandidateOut",
    "ScalpOutput",
    # Builders
    "build_scalp_prompt",
    "build_research_prompt",
    "build_quant_prompt",
    "build_risk_prompt",
    # Input types
    "ScalpInput",
    "ResearchInput",
    "QuantInput",
    "RiskInput",
]
