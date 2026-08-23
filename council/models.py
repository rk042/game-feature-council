from enum import Enum
from typing import Literal

from pydantic import BaseModel


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class GameDesignResult(BaseModel):
    agent: Literal["game_design"]

    recommendation: Literal[
        "support",
        "reduce_scope",
        "prototype_first",
        "needs_information",
        "oppose",
    ]

    player_problem: str
    player_value: str
    minimum_experience: str

    assumptions: list[str]
    risks: list[str]
    unnecessary_complexity: list[str]

    confidence: Confidence
    confidence_reason: str

class EffortRange(BaseModel):
    developer_days_min: float | None = None
    developer_days_max: float | None = None
    basis: str
    confidence: Confidence

class TechnicalResult(BaseModel):
    agent: Literal["technical"]

    recommendation: Literal[
        "support",
        "reduce_scope",
        "prototype_first",
        "needs_information",
        "oppose",
    ]

    systems_affected: list[str]
    reuse_points: list[str]

    new_code_required: list[str]

    service_dependencies: list[str]
    configuration_changes: list[str]
    qa_impact: list[str]
    integration_risks: list[str]

    assumptions: list[str]
    unknowns: list[str]

    effort: EffortRange

    confidence: Confidence
    confidence_reason: str