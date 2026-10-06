"""JSON schemas, prompt builders, and persona skill definitions.

Public API:
    schemas  — pydantic output models for each persona
    builders — pure-function prompt builders
"""

from arc.personas.builders import (
    AuditorInput,
    DirectorInput,
    InvestorInput,
    QuantInput,
    RiskInput,
    SweepInput,
    build_auditor_prompt,
    build_director_prompt,
    build_investor_prompt,
    build_quant_prompt,
    build_risk_prompt,
    build_sweep_prompt,
)
from arc.personas.schemas import (
    AnomalyReport,
    AuditorOutput,
    DirectorOutput,
    DirectorRankedItem,
    ImprovementStep,
    InvestorOutput,
    InvestorPlan,
    LessonLearned,
    QuantGreeks,
    QuantLeg,
    QuantOutput,
    QuantStructureOut,
    RiskAssessment,
    RiskOutput,
    SweepCandidateOut,
    SweepOutput,
)

__all__ = [
    # Schemas
    "AnomalyReport",
    "AuditorOutput",
    "DirectorOutput",
    "DirectorRankedItem",
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
    "SweepCandidateOut",
    "SweepOutput",
    # Builders
    "build_sweep_prompt",
    "build_director_prompt",
    "build_quant_prompt",
    "build_risk_prompt",
    "build_investor_prompt",
    "build_auditor_prompt",
    # Input types
    "SweepInput",
    "DirectorInput",
    "QuantInput",
    "RiskInput",
    "InvestorInput",
    "AuditorInput",
]
