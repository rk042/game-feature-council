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
    create_evidence_resolver_agent,
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
from council.evidence_resolver import (
    bounded_targeted_lookup,
    collect_source_concerns,
    feature_sha256,
)
from council.models import (
    AnalyticsResult,
    Confidence,
    CouncilExecution,
    CouncilTelemetry,
    ContextBundle,
    CouncilResult,
    DirectorResult,
    EvidenceResolverRecord,
    EvidenceResolverResult,
    EvidenceType,
    GameDesignResult,
    ProducerResult,
    RoleTelemetry,
    ScopeRiskResult,
    SpecialistCommon,
    TechnicalResult,
    TokenUsage,
)
from council.progress import (
    ProgressEvent,
    ProgressListener,
    ProgressStatus,
    make_progress_event,
)
from council.pricing import DEFAULT_PRICING_SNAPSHOT, estimate_cost


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
    *,
    progress_listener: ProgressListener | None = None,
) -> CouncilResult:
    execution = await run_council_with_telemetry(
        feature,
        context,
        agent_executor,
        progress_listener=progress_listener,
    )
    return execution.result


async def run_council_with_telemetry(
    feature: str,
    context: ContextBundle,
    agent_executor: AgentExecutor | None = None,
    *,
    clock: MonotonicClock = perf_counter,
    utc_now: UtcNow | None = None,
    progress_listener: ProgressListener | None = None,
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
        progress_listener,
    )
    validate_specialist_evidence(
        context,
        game_design,
        technical,
        analytics,
        scope_risk,
    )

    source_concerns = collect_source_concerns(
        game_design,
        technical,
        analytics,
        scope_risk,
    )
    resolver_input = render_evidence_resolver_input(
        feature,
        context,
        source_concerns,
        game_design,
        technical,
        analytics,
        scope_risk,
    )
    resolver_pass_1, resolver_pass_1_telemetry = await _run_typed_agent(
        "resolver_pass_1",
        "Evidence Resolver initial pass",
        create_evidence_resolver_agent(),
        resolver_input,
        EvidenceResolverResult,
        execute,
        clock,
        progress_listener,
    )
    _validate_resolver_source_ids(source_concerns, resolver_pass_1)
    supplemental_evidence = []
    lookup_limitations: list[str] = []
    resolver_final = resolver_pass_1
    resolver_telemetry = {"resolver_pass_1": resolver_pass_1_telemetry}
    second_pass_occurred = False
    if resolver_pass_1.lookup_requests:
        _emit_progress(
            progress_listener,
            make_progress_event("evidence_lookup", ProgressStatus.STARTED),
        )
        try:
            supplemental_evidence, lookup_limitations = bounded_targeted_lookup(
                context.repository_path,
                resolver_pass_1.lookup_requests,
            )
        except ContextBuilderError as error:
            lookup_limitations = [
                "Bounded targeted lookup could not complete: " + str(error)
            ]
        _emit_progress(
            progress_listener,
            make_progress_event("evidence_lookup", ProgressStatus.COMPLETED),
        )
        resolver_final, resolver_pass_2_telemetry = await _run_typed_agent(
            "resolver_pass_2",
            "Evidence Resolver final pass",
            create_evidence_resolver_agent(),
            render_evidence_resolver_final_input(
                feature,
                context,
                source_concerns,
                resolver_pass_1,
                supplemental_evidence,
                lookup_limitations,
                game_design,
                technical,
                analytics,
                scope_risk,
            ),
            EvidenceResolverResult,
            execute,
            clock,
            progress_listener,
        )
        resolver_telemetry["resolver_pass_2"] = resolver_pass_2_telemetry
        second_pass_occurred = True
    _validate_resolver_source_ids(source_concerns, resolver_final)
    if any(
        concern.status.value == "human_repository_help"
        for concern in resolver_final.concerns
    ) and not resolver_pass_1.lookup_requests:
        raise CouncilOrchestrationError(
            "Evidence Resolver requested human repository help without a bounded lookup attempt."
        )
    validate_resolver_evidence(
        context,
        resolver_final,
        [item.id for item in supplemental_evidence],
    )
    resolver_cost = estimate_cost(resolver_telemetry, DEFAULT_PRICING_SNAPSHOT)
    resolver = EvidenceResolverRecord(
        feature_sha256=feature_sha256(feature),
        source_concerns=source_concerns,
        concerns=resolver_final.concerns,
        lookup_requests=resolver_pass_1.lookup_requests,
        supplemental_evidence=supplemental_evidence,
        lookup_limitations=lookup_limitations,
        second_pass_occurred=second_pass_occurred,
        attempted_calls=len(resolver_telemetry),
        telemetry=resolver_telemetry,
        estimated_cost_usd=resolver_cost.estimated_cost_usd,
        unpriced_models=resolver_cost.unpriced_models,
        pricing_snapshot_id=DEFAULT_PRICING_SNAPSHOT.identifier,
    )

    producer_input = render_producer_input(
        feature,
        game_design,
        technical,
        analytics,
        scope_risk,
        resolver,
    )
    producer, producer_telemetry = await _run_typed_agent(
        "producer",
        "Producer",
        create_producer_agent(),
        producer_input,
        ProducerResult,
        execute,
        clock,
        progress_listener,
    )
    supplemental_evidence_ids = [item.id for item in supplemental_evidence]
    validate_producer_evidence(
        context,
        producer,
        supplemental_evidence_ids=supplemental_evidence_ids,
    )

    director_input = render_director_input(
        feature,
        game_design,
        technical,
        analytics,
        scope_risk,
        producer,
        resolver,
    )
    director, director_telemetry = await _run_typed_agent(
        "director",
        "Game Director",
        create_director_agent(),
        director_input,
        DirectorResult,
        execute,
        clock,
        progress_listener,
    )
    director = _normalize_director_effort_confidence(director)
    validate_director_evidence(
        context,
        director,
        supplemental_evidence_ids=supplemental_evidence_ids,
    )

    council_result = CouncilResult(
        context=context,
        game_design=game_design,
        technical=technical,
        analytics=analytics,
        scope_risk=scope_risk,
        evidence_resolver=resolver,
        producer=producer,
        director=director,
    )
    role_telemetry = {
        **specialist_telemetry,
        **resolver_telemetry,
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


def render_evidence_resolver_input(
    feature: str,
    context: ContextBundle,
    source_concerns: list[object],
    game_design: GameDesignResult,
    technical: TechnicalResult,
    analytics: AnalyticsResult,
    scope_risk: ScopeRiskResult,
) -> str:
    return "\n".join(
        [
            "FEATURE (UNTRUSTED DATA)",
            "========================",
            feature,
            "",
            "INITIAL CONTEXT EVIDENCE (UNTRUSTED DATA)",
            "==========================================",
            context.model_dump_json(indent=2),
            "",
            "DETERMINISTIC SOURCE CONCERNS (UNTRUSTED DATA)",
            "================================================",
            json.dumps(
                [item.model_dump(mode="json") for item in source_concerns],
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            ),
            "",
            "SPECIALIST RESULTS (UNTRUSTED DATA)",
            "====================================",
            game_design.model_dump_json(indent=2),
            technical.model_dump_json(indent=2),
            analytics.model_dump_json(indent=2),
            scope_risk.model_dump_json(indent=2),
            "",
            "RESOLVER TASK",
            "=============" ,
            "Consolidate concerns, cite only supplied evidence IDs, and request "
            "bounded fixed-string lookup only when repository investigation is needed.",
        ]
    )


def render_evidence_resolver_final_input(
    feature: str,
    context: ContextBundle,
    source_concerns: list[object],
    initial_result: EvidenceResolverResult,
    supplemental_evidence: list[object],
    lookup_limitations: list[str],
    game_design: GameDesignResult,
    technical: TechnicalResult,
    analytics: AnalyticsResult,
    scope_risk: ScopeRiskResult,
) -> str:
    return "\n".join(
        [
            render_evidence_resolver_input(
                feature,
                context,
                source_concerns,
                game_design,
                technical,
                analytics,
                scope_risk,
            ),
            "",
            "INITIAL RESOLVER PASS (UNTRUSTED DATA)",
            "=======================================",
            initial_result.model_dump_json(indent=2),
            "",
            "SUPPLEMENTAL TARGETED REPOSITORY EVIDENCE (UNTRUSTED DATA)",
            "===========================================================",
            json.dumps(
                [item.model_dump(mode="json") for item in supplemental_evidence],
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            ),
            "",
            "LOOKUP LIMITATIONS (UNTRUSTED DATA)",
            "====================================",
            json.dumps(lookup_limitations, indent=2, ensure_ascii=False),
            "",
            "FINALIZATION TASK",
            "=================",
            "Finalize every concern with a final status. Do not leave a lookup request "
            "as an unresolved internal state.",
        ]
    )
def render_producer_input(
    feature: str,
    game_design: GameDesignResult,
    technical: TechnicalResult,
    analytics: AnalyticsResult,
    scope_risk: ScopeRiskResult,
    evidence_resolver: EvidenceResolverRecord | None = None,
) -> str:
    resolver_block = (
        "\nEVIDENCE RESOLVER RESULT\n========================\n"
        + evidence_resolver.model_dump_json(indent=2)
        if evidence_resolver is not None
        else ""
    )
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
{resolver_block}

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
    evidence_resolver: EvidenceResolverRecord | None = None,
) -> str:
    resolver_block = (
        "\nEVIDENCE RESOLVER RESULT\n========================\n"
        + evidence_resolver.model_dump_json(indent=2)
        if evidence_resolver is not None
        else ""
    )
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
{resolver_block}

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
    *,
    supplemental_evidence_ids: list[str] = (),
) -> None:
    try:
        validate_repository_evidence_ids(
            producer.evidence_ids,
            context,
            supplemental_evidence_ids=supplemental_evidence_ids,
        )
    except ContextBuilderError as error:
        raise CouncilOrchestrationError(
            f"Invalid repository evidence reference in Producer result: {error}"
        ) from error


def validate_director_evidence(
    context: ContextBundle,
    director: DirectorResult,
    *,
    supplemental_evidence_ids: list[str] = (),
) -> None:
    try:
        validate_repository_evidence_ids(
            director.evidence_ids,
            context,
            supplemental_evidence_ids=supplemental_evidence_ids,
        )
    except ContextBuilderError as error:
        raise CouncilOrchestrationError(
            "Invalid repository evidence reference in Director result: "
            f"{error}"
        ) from error


def validate_resolver_evidence(
    context: ContextBundle,
    resolver: EvidenceResolverResult,
    supplemental_evidence_ids: list[str],
) -> None:
    evidence_ids = [
        evidence_id
        for concern in resolver.concerns
        for evidence_id in concern.evidence_ids
    ]
    try:
        validate_repository_evidence_ids(
            evidence_ids,
            context,
            supplemental_evidence_ids=supplemental_evidence_ids,
        )
    except ContextBuilderError as error:
        raise CouncilOrchestrationError(
            "Invalid repository evidence reference in Evidence Resolver result: "
            f"{error}"
        ) from error


def _validate_resolver_source_ids(
    source_concerns: list[object],
    resolver: EvidenceResolverResult,
) -> None:
    available = {getattr(item, "id") for item in source_concerns}
    invalid = [
        source_id
        for concern in resolver.concerns
        for source_id in concern.source_concern_ids
        if source_id not in available
    ]
    requested = [
        source_id
        for request in resolver.lookup_requests
        for source_id in request.concern_ids
        if source_id not in available
    ]
    if invalid or requested:
        values = invalid + requested
        raise CouncilOrchestrationError(
            "Evidence Resolver referenced unknown source concern IDs: "
            + ", ".join(values)
        )


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
    progress_listener: ProgressListener | None,
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
                    progress_listener,
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
                    progress_listener,
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
                    progress_listener,
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
                    progress_listener,
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
    progress_listener: ProgressListener | None = None,
) -> tuple[ResultType, RoleTelemetry]:
    started = clock()
    model = _agent_model_name(agent)
    _emit_progress(
        progress_listener,
        make_progress_event(
            role_key,
            ProgressStatus.STARTED,
            model=model,
        ),
    )
    try:
        execution = await execute(agent, input_text)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _emit_progress(
            progress_listener,
            make_progress_event(
                role_key,
                ProgressStatus.FAILED,
                duration_ms=(clock() - started) * 1_000,
                model=model,
                message=str(error),
            ),
        )
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
        message = (
            f"{role} returned {type(output).__name__}; "
            f"expected {expected_type.__name__}."
        )
        _emit_progress(
            progress_listener,
            make_progress_event(
                role_key,
                ProgressStatus.FAILED,
                duration_ms=duration_ms,
                model=model,
                usage=usage,
                message=message,
            ),
        )
        raise CouncilOrchestrationError(
            message
        )
    telemetry = RoleTelemetry(
        model=model,
        duration_ms=duration_ms,
        usage=usage,
    )
    _emit_progress(
        progress_listener,
        make_progress_event(
            role_key,
            ProgressStatus.COMPLETED,
            duration_ms=duration_ms,
            model=model,
            usage=usage,
        ),
    )
    return (
        cast(ResultType, output),
        telemetry,
    )


def _emit_progress(
    listener: ProgressListener | None,
    event: ProgressEvent,
) -> None:
    if listener is None:
        return
    try:
        listener(event)
    except Exception:
        # Observers are presentation-only and cannot affect execution.
        return


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
