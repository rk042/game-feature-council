from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceType(str, Enum):
    REPOSITORY = "repository"
    PROJECT_DOCUMENTATION = "project_documentation"
    USER_ASSUMPTION = "user_assumption"
    AGENT_INFERENCE = "agent_inference"


class EvidenceItem(BaseModel):
    id: str
    source_type: EvidenceType
    claim: str
    file_path: str | None = None
    symbol: str | None = None
    reason_it_matters: str
    supports: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)


class EffortRange(BaseModel):
    developer_days_min: float | None = None
    developer_days_max: float | None = None
    basis: str
    confidence: Confidence


class EventSpec(BaseModel):
    event: str
    purpose: str
    properties: list[str] = Field(default_factory=list)


class OutcomeInterpretation(BaseModel):
    observed_pattern: str
    interpretation: str
    action: Literal[
        "scale",
        "iterate",
        "kill",
        "need_more_data",
    ]


class ExperimentDefinition(BaseModel):
    player_problem: str
    hypothesis: str
    riskiest_assumption: str
    smallest_experiment: str

    build: list[str]
    do_not_build: list[str]

    behavioural_question: str
    events: list[EventSpec]
    funnel: list[str]
    success_metrics: list[str]
    failure_signals: list[str]

    observation_window: str
    segments: list[str]

    learning_cases: list[OutcomeInterpretation]
    decision_threshold_notes: list[str]


class SpecialistCommon(BaseModel):
    agent: Literal[
        "game_design",
        "technical",
        "analytics",
        "scope_risk",
    ]

    recommendation: Literal[
        "support",
        "reduce_scope",
        "prototype_first",
        "needs_information",
        "oppose",
    ]

    confidence: Confidence
    confidence_reason: str

    assumptions: list[str]
    findings: list[Finding]
    risks: list[str]
    unknowns: list[str]
    evidence: list[EvidenceItem]


class GameDesignResult(SpecialistCommon):
    agent: Literal["game_design"]

    player_problem: str
    player_value: str
    core_loop: list[str]
    minimum_experience: str
    unnecessary_complexity: list[str]


class TechnicalResult(SpecialistCommon):
    agent: Literal["technical"]

    systems_affected: list[str]
    reuse_points: list[str]
    new_code_required: list[str]
    service_dependencies: list[str]
    configuration_changes: list[str]
    qa_impact: list[str]
    integration_risks: list[str]

    effort: EffortRange


class AnalyticsResult(SpecialistCommon):
    agent: Literal["analytics"]

    experiment: ExperimentDefinition

class ScopeRiskResult(SpecialistCommon):
    agent: Literal["scope_risk"]

    cheapest_credible_experiment: str
    removable_scope: list[str]
    cheaper_test_options: list[str]
    data_invalidation_risks: list[str]
    player_trust_risks: list[str]
    economy_risks: list[str]
    technical_risks: list[str]