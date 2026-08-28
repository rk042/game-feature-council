import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import TypeVar, cast

from agents import Agent
from pydantic import BaseModel

from council.agents import (
    create_analytics_agent,
    create_director_agent,
    create_game_design_agent,
    create_producer_agent,
    create_scope_risk_agent,
    create_technical_agent,
)
from council.execution import AgentCallResult, execute_agent
from council.context import (
    ContextBuilderError,
    validate_repository_evidence_ids,
)
from council.models import (
    AnalyticsResult,
    Confidence,
    CouncilExecution,
    CouncilTelemetry,
    ContextBundle,
    CouncilResult,
    DirectorResult,
    EvidenceType,
    GameDesignResult,
    ProducerResult,
    RoleTelemetry,
    ScopeRiskResult,
    SpecialistCommon,
    TechnicalResult,
    TokenUsage,
)


_execute_agent = execute_agent


AgentExecutor = Callable[
    [Agent, str],
    Awaitable[BaseModel | AgentCallResult],
]
MonotonicClock = Callable[[], float]
UtcNow = Callable[[], datetime]
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
    execution = await run_council_with_telemetry(
        feature,
        context,
        agent_executor,
    )
    return execution.result


async def run_council_with_telemetry(
    feature: str,
    context: ContextBundle,
    agent_executor: AgentExecutor | None = None,
    *,
    clock: MonotonicClock = perf_counter,
    utc_now: UtcNow | None = None,
) -> CouncilExecution:
    execute = agent_executor or _execute_agent
    started_at = (utc_now or _utc_now)()
    total_started = clock()
    specialist_input = render_specialist_input(feature, context)

    (
        game_design,
        technical,
        analytics,
        scope_risk,
        specialist_telemetry,
    ) = await _run_specialists(
        specialist_input,
        execute,
        clock,
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
    producer, producer_telemetry = await _run_typed_agent(
        "producer",
        "Producer",
        create_producer_agent(),
        producer_input,
        ProducerResult,
        execute,
        clock,
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
    director, director_telemetry = await _run_typed_agent(
        "director",
        "Game Director",
        create_director_agent(),
        director_input,
        DirectorResult,
        execute,
        clock,
    )
    director = _normalize_director_effort_confidence(director)

    council_result = CouncilResult(
        context=context,
        game_design=game_design,
        technical=technical,
        analytics=analytics,
        scope_risk=scope_risk,
        producer=producer,
        director=director,
    )
    role_telemetry = {
        **specialist_telemetry,
        "producer": producer_telemetry,
        "director": director_telemetry,
    }
    return CouncilExecution(
        result=council_result,
        telemetry=CouncilTelemetry(
            started_at=started_at,
            total_duration_ms=(clock() - total_started) * 1_000,
            roles=role_telemetry,
            total_usage=_aggregate_usage(role_telemetry),
        ),
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


def _normalize_director_effort_confidence(
    director: DirectorResult,
) -> DirectorResult:
    effort = director.effort
    if (
        effort.developer_days_min is not None
        or effort.developer_days_max is not None
        or effort.confidence == Confidence.LOW
    ):
        return director

    return director.model_copy(
        update={
            "effort": effort.model_copy(
                update={"confidence": Confidence.LOW}
            )
        }
    )


async def _run_specialists(
    specialist_input: str,
    execute: AgentExecutor,
    clock: MonotonicClock,
) -> tuple[
    GameDesignResult,
    TechnicalResult,
    AnalyticsResult,
    ScopeRiskResult,
    dict[str, RoleTelemetry],
]:
    try:
        async with asyncio.TaskGroup() as task_group:
            game_design_task = task_group.create_task(
                _run_typed_agent(
                    "game_design",
                    "Game Design specialist",
                    create_game_design_agent(),
                    specialist_input,
                    GameDesignResult,
                    execute,
                    clock,
                )
            )
            technical_task = task_group.create_task(
                _run_typed_agent(
                    "technical",
                    "Technical specialist",
                    create_technical_agent(),
                    specialist_input,
                    TechnicalResult,
                    execute,
                    clock,
                )
            )
            analytics_task = task_group.create_task(
                _run_typed_agent(
                    "analytics",
                    "Analytics specialist",
                    create_analytics_agent(),
                    specialist_input,
                    AnalyticsResult,
                    execute,
                    clock,
                )
            )
            scope_risk_task = task_group.create_task(
                _run_typed_agent(
                    "scope_risk",
                    "Scope / Risk specialist",
                    create_scope_risk_agent(),
                    specialist_input,
                    ScopeRiskResult,
                    execute,
                    clock,
                )
            )
    except* CouncilOrchestrationError as error_group:
        messages = [str(error) for error in error_group.exceptions]
        raise CouncilOrchestrationError(
            "Specialist phase failed: " + "; ".join(messages)
        ) from error_group

    game_design, game_design_telemetry = game_design_task.result()
    technical, technical_telemetry = technical_task.result()
    analytics, analytics_telemetry = analytics_task.result()
    scope_risk, scope_risk_telemetry = scope_risk_task.result()
    return (
        game_design,
        technical,
        analytics,
        scope_risk,
        {
            "game_design": game_design_telemetry,
            "technical": technical_telemetry,
            "analytics": analytics_telemetry,
            "scope_risk": scope_risk_telemetry,
        },
    )


async def _run_typed_agent(
    role_key: str,
    role: str,
    agent: Agent,
    input_text: str,
    expected_type: type[ResultType],
    execute: AgentExecutor,
    clock: MonotonicClock,
) -> tuple[ResultType, RoleTelemetry]:
    started = clock()
    try:
        execution = await execute(agent, input_text)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise CouncilOrchestrationError(
            f"{role} execution failed: {error}"
        ) from error

    duration_ms = (clock() - started) * 1_000
    if isinstance(execution, AgentCallResult):
        output = execution.output
        usage = execution.usage
    else:
        output = execution
        usage = TokenUsage()

    if not isinstance(output, expected_type):
        raise CouncilOrchestrationError(
            f"{role} returned {type(output).__name__}; "
            f"expected {expected_type.__name__}."
        )
    return (
        cast(ResultType, output),
        RoleTelemetry(
            model=_agent_model_name(agent),
            duration_ms=duration_ms,
            usage=usage,
        ),
    )


def _aggregate_usage(
    roles: dict[str, RoleTelemetry],
) -> TokenUsage:
    return TokenUsage(
        requests=sum(role.usage.requests for role in roles.values()),
        input_tokens=sum(role.usage.input_tokens for role in roles.values()),
        output_tokens=sum(role.usage.output_tokens for role in roles.values()),
        total_tokens=sum(role.usage.total_tokens for role in roles.values()),
    )


def _agent_model_name(agent: Agent) -> str:
    model = getattr(agent, "model", None)
    if isinstance(model, str):
        return model
    if model is not None:
        model_name = getattr(model, "model", None) or getattr(model, "name", None)
        if isinstance(model_name, str):
            return model_name
        return type(model).__name__
    if isinstance(agent, str):
        return agent
    return type(agent).__name__


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
