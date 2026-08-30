from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


class ContextBuilderConfig(BaseModel):
    max_search_terms: int = Field(default=12, gt=0)
    max_selected_files: int = Field(default=10, gt=0)
    max_characters_per_file: int = Field(default=8_000, gt=0)
    max_total_characters: int = Field(default=50_000, gt=0)


class RepositoryEvidence(BaseModel):
    id: str
    file_path: str
    selection_reasons: list[str]
    matched_terms: list[str] = Field(default_factory=list)
    text: str
    truncated: bool


class ContextBundle(BaseModel):
    repository_path: str
    commit_sha: str
    branch: str | None
    working_tree_dirty: bool

    feature_input: str
    search_terms: list[str]
    configuration: ContextBuilderConfig

    tracked_file_count: int
    candidate_file_count: int
    selected_file_count: int
    skipped_file_count: int
    total_text_characters: int
    truncated_file_count: int
    selection_limited: bool

    evidence: list[RepositoryEvidence]


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

class ProducerResult(BaseModel):
    confidence: Confidence
    confidence_reason: str

    agreements: list[str]
    disagreements: list[str]
    dependencies: list[str]
    scope_conflicts: list[str]
    risk_conflicts: list[str]

    unknowns: list[str]
    missing_repository_information: list[str]
    human_decisions_required: list[str]

    proposed_experiment: ExperimentDefinition
    execution_steps: list[str]
    effort: EffortRange

    evidence_ids: list[str]

class DirectorDecision(str, Enum):
    GO = "GO"
    GO_WITH_REDUCED_SCOPE = "GO_WITH_REDUCED_SCOPE"
    PROTOTYPE_FIRST = "PROTOTYPE_FIRST"
    NEEDS_MORE_INFORMATION = "NEEDS_MORE_INFORMATION"
    DO_NOT_BUILD_YET = "DO_NOT_BUILD_YET"


class DecisionConditions(BaseModel):
    scale: list[str]
    iterate: list[str]
    kill: list[str]
    need_more_data: list[str]


class DirectorResult(BaseModel):
    decision: DirectorDecision

    confidence: Confidence
    confidence_reason: str
    rationale: list[str]

    experiment: ExperimentDefinition
    effort: EffortRange

    unresolved_unknowns: list[str]
    human_decisions_required: list[str]

    decision_conditions: DecisionConditions

    human_approval_required: Literal[True] = True


class CouncilResult(BaseModel):
    context: ContextBundle
    game_design: GameDesignResult
    technical: TechnicalResult
    analytics: AnalyticsResult
    scope_risk: ScopeRiskResult
    producer: ProducerResult
    director: DirectorResult


class FeatureRefinerResult(BaseModel):
    concise_interpretation: list[str] = Field(min_length=1)
    refined_brief: str = Field(min_length=1)
    unresolved_points: list[str] = Field(default_factory=list)
    preserved_constraints: list[str] = Field(default_factory=list)

    @field_validator(
        "concise_interpretation",
        "unresolved_points",
        "preserved_constraints",
    )
    @classmethod
    def reject_blank_list_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("feature-refinement list items must not be blank")
        return values

    @field_validator("refined_brief")
    @classmethod
    def reject_blank_refined_brief(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("refined brief must not be blank")
        return value


class TokenUsage(BaseModel):
    requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class RoleTelemetry(BaseModel):
    model: str
    duration_ms: float = Field(ge=0)
    usage: TokenUsage


class FeatureRefinementExecution(BaseModel):
    result: FeatureRefinerResult
    telemetry: RoleTelemetry
    estimated_cost_usd: Decimal | None
    unpriced_models: list[str] = Field(default_factory=list)
    pricing_snapshot_id: str


class FeatureRefinementRound(BaseModel):
    result: FeatureRefinerResult
    user_correction: str | None = None
    telemetry: RoleTelemetry
    estimated_cost_usd: Decimal | None
    unpriced_models: list[str] = Field(default_factory=list)
    pricing_snapshot_id: str

    @field_validator("user_correction")
    @classmethod
    def reject_blank_correction(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("user correction must not be blank")
        return value


class FeatureRefinementRecord(BaseModel):
    original_feature: str = Field(min_length=1)
    approved_refined_feature: str | None = None
    concise_interpretation: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    preserved_constraints: list[str] = Field(default_factory=list)
    refinement_rounds: int = Field(default=0, ge=0)
    rounds: list[FeatureRefinementRound] = Field(default_factory=list)
    attempted_calls: int = Field(default=0, ge=0)
    approved: bool
    usage_complete: bool = True
    usage_unavailable_reason: str | None = None
    telemetry: RoleTelemetry
    estimated_cost_usd: Decimal | None
    unpriced_models: list[str] = Field(default_factory=list)
    pricing_snapshot_id: str

    @field_validator("original_feature")
    @classmethod
    def reject_blank_original_feature(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("original feature must not be blank")
        return value

    @model_validator(mode="after")
    def validate_refinement_audit(self) -> "FeatureRefinementRecord":
        if self.refinement_rounds != len(self.rounds):
            raise ValueError("refinement round count does not match history")
        # Phase 4A artifacts written before attempted_calls existed contain
        # completed rounds only. Treat that absence as complete usage rather
        # than inventing an unavailable attempt during backward-compatible
        # loading.
        if (
            self.attempted_calls == 0
            and self.refinement_rounds > 0
            and self.usage_complete
        ):
            self.attempted_calls = self.refinement_rounds
        if self.attempted_calls < self.refinement_rounds:
            raise ValueError("attempted calls cannot be fewer than completed rounds")
        if self.rounds:
            latest = self.rounds[-1].result
            if self.concise_interpretation != latest.concise_interpretation:
                raise ValueError("concise interpretation must match latest round")
            if self.unresolved_points != latest.unresolved_points:
                raise ValueError("unresolved points must match latest round")
            if self.preserved_constraints != latest.preserved_constraints:
                raise ValueError("preserved constraints must match latest round")
        elif (
            self.concise_interpretation
            or self.unresolved_points
            or self.preserved_constraints
        ):
            raise ValueError("a failed first attempt cannot have an interpretation")
        if self.approved:
            if not self.rounds:
                raise ValueError("approved refinement requires a completed round")
            if not self.approved_refined_feature or not self.approved_refined_feature.strip():
                raise ValueError("approved refinement requires an approved brief")
        elif self.approved_refined_feature is not None:
            raise ValueError("unapproved refinement cannot have an approved brief")
        if self.usage_complete:
            if self.usage_unavailable_reason is not None:
                raise ValueError("complete usage cannot have an unavailable reason")
            if self.attempted_calls != self.refinement_rounds:
                raise ValueError("complete usage requires all attempts to be recorded")
        elif not (
            self.usage_unavailable_reason
            and self.usage_unavailable_reason.strip()
        ):
            raise ValueError("incomplete usage requires an unavailable reason")
        elif self.attempted_calls <= self.refinement_rounds:
            raise ValueError("incomplete usage requires an unavailable attempt")
        return self


class CouncilTelemetry(BaseModel):
    started_at: datetime
    total_duration_ms: float = Field(ge=0)
    roles: dict[str, RoleTelemetry]
    total_usage: TokenUsage

    @field_validator("started_at")
    @classmethod
    def normalize_started_at(cls, value: datetime) -> datetime:
        return _as_utc(value)


class CouncilExecution(BaseModel):
    result: CouncilResult
    telemetry: CouncilTelemetry


class HumanAction(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    MODIFY = "modify"


class HumanDecision(BaseModel):
    action: HumanAction
    final_decision: DirectorDecision | None = None
    note: str | None = None
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def validate_final_decision(self) -> "HumanDecision":
        if self.action in {HumanAction.ACCEPT, HumanAction.MODIFY}:
            if self.final_decision is None:
                raise ValueError(
                    f"{self.action.value} requires a final decision"
                )
        return self


class RunRecord(BaseModel):
    run_id: str
    feature_input: str

    repository_path: str
    repository_commit_sha: str
    repository_branch: str | None
    working_tree_dirty: bool

    telemetry: CouncilTelemetry
    estimated_cost_usd: Decimal | None
    unpriced_models: list[str] = Field(default_factory=list)
    pricing_snapshot_id: str

    council_result: CouncilResult
    ai_recommendation: DirectorDecision
    human_decision: HumanDecision | None = None


class ContextEvidenceIdentity(BaseModel):
    id: str
    file_path: str
    truncated: bool


class EvaluationContextIdentity(BaseModel):
    repository_path: str
    commit_sha: str
    branch: str | None
    working_tree_dirty: bool
    evidence_manifest: list[ContextEvidenceIdentity]
    context_sha256: str


class GeneralistExecution(BaseModel):
    feature_sha256: str
    context_identity: EvaluationContextIdentity
    result: DirectorResult
    telemetry: RoleTelemetry
    estimated_cost_usd: Decimal | None
    unpriced_models: list[str] = Field(default_factory=list)
    pricing_snapshot_id: str


class ComparisonPathMetrics(BaseModel):
    duration_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal | None
    decision: DirectorDecision
    confidence: Confidence


class ComparisonPreference(str, Enum):
    GENERALIST = "generalist"
    COUNCIL = "council"
    TIE = "tie"


class ComparisonRubricScores(BaseModel):
    grounding: int = Field(ge=1, le=5, strict=True)
    scope_reduction: int = Field(ge=1, le=5, strict=True)
    hypothesis_quality: int = Field(ge=1, le=5, strict=True)
    experiment_credibility: int = Field(ge=1, le=5, strict=True)
    measurement_to_learning_logic: int = Field(ge=1, le=5, strict=True)
    technical_realism: int = Field(ge=1, le=5, strict=True)
    decision_usefulness: int = Field(ge=1, le=5, strict=True)
    conciseness: int = Field(ge=1, le=5, strict=True)


class InsightComparison(BaseModel):
    issue: str
    generalist_observation: str
    council_observation: str
    assessment: str


class HumanComparisonReview(BaseModel):
    generalist_scores: ComparisonRubricScores
    council_scores: ComparisonRubricScores
    preference: ComparisonPreference
    reason: str = Field(min_length=1)
    insights: list[InsightComparison] = Field(default_factory=list)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _as_utc(value)


class ComparisonRecord(BaseModel):
    council_run_id: str
    feature_sha256: str
    context_identity: EvaluationContextIdentity
    generalist: GeneralistExecution
    council_metrics: ComparisonPathMetrics
    generalist_metrics: ComparisonPathMetrics
    human_review: HumanComparisonReview | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
