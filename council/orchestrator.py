import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from agents import Agent, Runner
from pydantic import BaseModel

from council.agents import (
    create_analytics_agent,
    create_director_agent,
    create_game_design_agent,
    create_producer_agent,
    create_scope_risk_agent,
    create_technical_agent,
)
from council.context import (
    ContextBuilderError,
    validate_repository_evidence_ids,
)
from council.models import (
    AnalyticsResult,
    ContextBundle,
    CouncilResult,
    DirectorResult,
    EvidenceType,
    GameDesignResult,
    ProducerResult,
    ScopeRiskResult,
    SpecialistCommon,
    TechnicalResult,
)


AgentExecutor = Callable[[Agent, str], Awaitable[BaseModel]]
SpecialistResult = (
    GameDesignResult
    | TechnicalResult
    | AnalyticsResult
    | ScopeRiskResult
)
ResultType = TypeVar("ResultType", bound=BaseModel)


class CouncilOrchestrationError(RuntimeError):
    pass


async def run_council(
    feature: str,
    context: ContextBundle,
    agent_executor: AgentExecutor | None = None,
) -> CouncilResult:
    execute = agent_executor or _execute_agent
    specialist_input = render_specialist_input(feature, context)

    game_design, technical, analytics, scope_risk = await _run_specialists(
        specialist_input,
        execute,
    )
    validate_specialist_evidence(
        context,
        game_design,
        technical,
        analytics,
        scope_risk,
    )

    producer_input = render_producer_input(
        feature,
        game_design,
        technical,
        analytics,
        scope_risk,
    )
    producer = await _run_typed_agent(
        "Producer",
        create_producer_agent(),
        producer_input,
        ProducerResult,
        execute,
    )
    validate_producer_evidence(context, producer)

    director_input = render_director_input(
        feature,
        game_design,
        technical,
        analytics,
        scope_risk,
        producer,
    )
    director = await _run_typed_agent(
        "Game Director",
        create_director_agent(),
        director_input,
        DirectorResult,
        execute,
    )

    return CouncilResult(
        context=context,
        game_design=game_design,
        technical=technical,
        analytics=analytics,
        scope_risk=scope_risk,
        producer=producer,
        director=director,
    )


def render_specialist_input(feature: str, context: ContextBundle) -> str:
    provenance = context.model_dump(
        mode="json",
        exclude={"feature_input", "evidence"},
    )
    evidence = [item.model_dump(mode="json") for item in context.evidence]

    return "\n".join(
        [
            "FEATURE / USER INPUT (NOT REPOSITORY EVIDENCE)",
            "================================================",
            feature,
            "",
            "REPOSITORY PROVENANCE",
            "=====================",
            json.dumps(provenance, indent=2, sort_keys=True, ensure_ascii=False),
            "",
            "BOUNDED REPOSITORY EVIDENCE",
            "===========================",
            json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False),
        ]
    )


def render_producer_input(
    feature: str,
    game_design: GameDesignResult,
    technical: TechnicalResult,
    analytics: AnalyticsResult,
    scope_risk: ScopeRiskResult,
) -> str:
    return f"""FEATURE
-------
{feature}

GAME DESIGN RESULT
==================
{game_design.model_dump_json(indent=2)}

TECHNICAL RESULT
================
{technical.model_dump_json(indent=2)}

ANALYTICS RESULT
================
{analytics.model_dump_json(indent=2)}

SCOPE / RISK RESULT
===================
{scope_risk.model_dump_json(indent=2)}

PRODUCER TASK
=============
Create one coherent executable experiment from the four structured
specialist results above.

Preserve material disagreements.

Do not invent repository facts, evidence IDs, implementation effort,
or numeric decision thresholds.
"""


def render_director_input(
    feature: str,
    game_design: GameDesignResult,
    technical: TechnicalResult,
    analytics: AnalyticsResult,
    scope_risk: ScopeRiskResult,
    producer: ProducerResult,
) -> str:
    return f"""FEATURE
-------
{feature}

GAME DESIGN RESULT
==================
{game_design.model_dump_json(indent=2)}

TECHNICAL RESULT
================
{technical.model_dump_json(indent=2)}

ANALYTICS RESULT
================
{analytics.model_dump_json(indent=2)}

SCOPE / RISK RESULT
===================
{scope_risk.model_dump_json(indent=2)}

PRODUCER RESULT
===============
{producer.model_dump_json(indent=2)}

DIRECTOR TASK
=============
Judge the Producer's proposed experiment.

Do not redo the specialist analyses.

Judge expected validated learning against effort, evidence quality,
risk, specialist disagreement, and unresolved uncertainty.

Do not invent repository facts, implementation effort,
numeric thresholds, or production approval.
"""


def validate_specialist_evidence(
    context: ContextBundle,
    game_design: GameDesignResult,
    technical: TechnicalResult,
    analytics: AnalyticsResult,
    scope_risk: ScopeRiskResult,
) -> None:
    specialists: tuple[tuple[str, SpecialistResult], ...] = (
        ("game_design", game_design),
        ("technical", technical),
        ("analytics", analytics),
        ("scope_risk", scope_risk),
    )

    for role, result in specialists:
        evidence_ids = _collect_repository_evidence_ids(result)
        try:
            validate_repository_evidence_ids(evidence_ids, context)
        except ContextBuilderError as error:
            raise CouncilOrchestrationError(
                f"Invalid repository evidence reference in {role} result: "
                f"{error}"
            ) from error


def validate_producer_evidence(
    context: ContextBundle,
    producer: ProducerResult,
) -> None:
    try:
        validate_repository_evidence_ids(producer.evidence_ids, context)
    except ContextBuilderError as error:
        raise CouncilOrchestrationError(
            f"Invalid repository evidence reference in Producer result: {error}"
        ) from error


async def _run_specialists(
    specialist_input: str,
    execute: AgentExecutor,
) -> tuple[
    GameDesignResult,
    TechnicalResult,
    AnalyticsResult,
    ScopeRiskResult,
]:
    try:
        async with asyncio.TaskGroup() as task_group:
            game_design_task = task_group.create_task(
                _run_typed_agent(
                    "Game Design specialist",
                    create_game_design_agent(),
                    specialist_input,
                    GameDesignResult,
                    execute,
                )
            )
            technical_task = task_group.create_task(
                _run_typed_agent(
                    "Technical specialist",
                    create_technical_agent(),
                    specialist_input,
                    TechnicalResult,
                    execute,
                )
            )
            analytics_task = task_group.create_task(
                _run_typed_agent(
                    "Analytics specialist",
                    create_analytics_agent(),
                    specialist_input,
                    AnalyticsResult,
                    execute,
                )
            )
            scope_risk_task = task_group.create_task(
                _run_typed_agent(
                    "Scope / Risk specialist",
                    create_scope_risk_agent(),
                    specialist_input,
                    ScopeRiskResult,
                    execute,
                )
            )
    except* CouncilOrchestrationError as error_group:
        messages = [str(error) for error in error_group.exceptions]
        raise CouncilOrchestrationError(
            "Specialist phase failed: " + "; ".join(messages)
        ) from error_group

    return (
        game_design_task.result(),
        technical_task.result(),
        analytics_task.result(),
        scope_risk_task.result(),
    )


async def _execute_agent(agent: Agent, input_text: str) -> BaseModel:
    result = await Runner.run(agent, input_text)
    return result.final_output


async def _run_typed_agent(
    role: str,
    agent: Agent,
    input_text: str,
    expected_type: type[ResultType],
    execute: AgentExecutor,
) -> ResultType:
    try:
        output = await execute(agent, input_text)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise CouncilOrchestrationError(
            f"{role} execution failed: {error}"
        ) from error

    if not isinstance(output, expected_type):
        raise CouncilOrchestrationError(
            f"{role} returned {type(output).__name__}; "
            f"expected {expected_type.__name__}."
        )
    return cast(ResultType, output)


def _collect_repository_evidence_ids(
    specialist: SpecialistCommon,
) -> list[str]:
    source_types_by_id: dict[str, set[EvidenceType]] = {}
    repository_ids: list[str] = []

    for item in specialist.evidence:
        source_types_by_id.setdefault(item.id, set()).add(item.source_type)
        if item.source_type == EvidenceType.REPOSITORY:
            repository_ids.append(item.id)

    for finding in specialist.findings:
        for evidence_id in finding.evidence_ids:
            source_types = source_types_by_id.get(evidence_id)
            if source_types is not None:
                if EvidenceType.REPOSITORY in source_types:
                    repository_ids.append(evidence_id)
            elif evidence_id.startswith("repo-"):
                repository_ids.append(evidence_id)

    return list(dict.fromkeys(repository_ids))
