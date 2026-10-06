"""JSON schemas, prompt builders, and persona skill definitions.

Public API:
    schemas  — pydantic output models for each persona
    builders — pure-function prompt builders
"""

from arc.personas.builders import (
    AuditorInput,
    InvestorInput,
    QuantInput,
    ResearchInput,
    RiskInput,
    ScalpInput,
    build_auditor_prompt,
    build_investor_prompt,
    build_quant_prompt,
    build_research_prompt,
    build_risk_prompt,
    build_scalp_prompt,
)
from arc.personas.schemas import (
    AnomalyReport,
    AuditorOutput,
    ImprovementStep,
    InvestorOutput,
    InvestorPlan,
    LessonLearned,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantStructureOut,
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
    "AuditorOutput",
    "ResearchOutput",
    "ResearchRankedItem",
    "InvestorOutput",
    "InvestorPlan",
    "ImprovementStep",
    "LessonLearned",
    "QuantGreeks",
    "QuantLeg",
    "QuantOutput",
    "QuantStructureOut",
    "RiskAssessment",
    "RiskOutput",
    "ScalpCandidateOut",
    "ScalpOutput",
    # Builders
    "build_scalp_prompt",
    "build_research_prompt",
    "build_quant_prompt",
    "build_risk_prompt",
    "build_investor_prompt",
    "build_auditor_prompt",
    # Input types
    "ScalpInput",
    "ResearchInput",
    "QuantInput",
    "RiskInput",
    "InvestorInput",
    "AuditorInput",
]
