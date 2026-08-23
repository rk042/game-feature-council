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