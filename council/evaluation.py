from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from time import perf_counter
from typing import TYPE_CHECKING

from council.models import (
    ComparisonPathMetrics,
    ComparisonPreference,
    ComparisonRecord,
    ComparisonRubricScores,
    ContextBundle,
    ContextEvidenceIdentity,
    DirectorResult,
    EvaluationContextIdentity,
    GeneralistExecution,
    HumanComparisonReview,
    RunRecord,
)
from council.pricing import (
    DEFAULT_PRICING_SNAPSHOT,
    PricingSnapshot,
    estimate_cost,
)

if TYPE_CHECKING:
    from council.orchestrator import AgentExecutor, MonotonicClock
    from council.progress import ProgressListener


RUBRIC_DIMENSIONS = (
    ("grounding", "Grounding"),
    ("scope_reduction", "Scope reduction"),
    ("hypothesis_quality", "Hypothesis quality"),
    ("experiment_credibility", "Experiment credibility"),
    ("measurement_to_learning_logic", "Measurement-to-learning logic"),
    ("technical_realism", "Technical realism"),
    ("decision_usefulness", "Decision usefulness"),
    ("conciseness", "Conciseness"),
)


class EvaluationError(RuntimeError):
    pass


async def run_generalist(
    feature: str,
    context: ContextBundle,
    agent_executor: AgentExecutor | None = None,
    *,
    pricing: PricingSnapshot = DEFAULT_PRICING_SNAPSHOT,
    clock: MonotonicClock = perf_counter,
    progress_listener: ProgressListener | None = None,
) -> GeneralistExecution:
    from council.context import (
        ContextBuilderError,
        validate_repository_evidence_ids,
    )
    from council.orchestrator import (
        CouncilOrchestrationError,
        _execute_agent,
        _run_typed_agent,
        render_specialist_input,
    )

    execute = agent_executor or _execute_agent
    generalist_input = render_specialist_input(feature, context)

    try:
        result, telemetry = await _run_typed_agent(
            "generalist",
            "Generalist baseline",
            create_generalist_agent(),
            generalist_input,
            DirectorResult,
            execute,
            clock,
            progress_listener,
        )
    except CouncilOrchestrationError as error:
        raise EvaluationError(f"Generalist execution failed: {error}") from error
    try:
        # Generalist fairness deliberately permits only the initial canonical
        # ContextBundle evidence, never Council supplemental lookup evidence.
        validate_repository_evidence_ids(result.evidence_ids, context)
    except ContextBuilderError as error:
        raise EvaluationError(
            "Invalid repository evidence reference in Generalist result: "
            f"{error}"
        ) from error

    cost = estimate_cost({"generalist": telemetry}, pricing)
    return GeneralistExecution(
        feature_sha256=_text_sha256(feature),
        context_identity=build_context_identity(context),
        result=result,
        telemetry=telemetry,
        estimated_cost_usd=cost.estimated_cost_usd,
        unpriced_models=cost.unpriced_models,
        pricing_snapshot_id=pricing.identifier,
    )


def create_generalist_agent():
    from council.agents import create_generalist_agent as create_agent

    return create_agent()


def create_comparison_record(
    council_run: RunRecord,
    generalist: GeneralistExecution,
) -> ComparisonRecord:
    expected_feature_sha256 = _text_sha256(council_run.feature_input)
    expected_context_identity = build_context_identity(
        council_run.council_result.context
    )

    if generalist.feature_sha256 != expected_feature_sha256:
        raise EvaluationError(
            "Generalist feature input does not match the Council feature input."
        )
    if generalist.context_identity != expected_context_identity:
        raise EvaluationError(
            "Generalist ContextBundle does not match the Council ContextBundle."
        )

    council_usage = council_run.telemetry.total_usage
    generalist_usage = generalist.telemetry.usage
    return ComparisonRecord(
        council_run_id=council_run.run_id,
        feature_sha256=expected_feature_sha256,
        context_identity=expected_context_identity,
        generalist=generalist,
        council_metrics=ComparisonPathMetrics(
            duration_ms=council_run.telemetry.total_duration_ms,
            input_tokens=council_usage.input_tokens,
            output_tokens=council_usage.output_tokens,
            total_tokens=council_usage.total_tokens,
            estimated_cost_usd=council_run.estimated_cost_usd,
            decision=council_run.council_result.director.decision,
            confidence=council_run.council_result.director.confidence,
        ),
        generalist_metrics=ComparisonPathMetrics(
            duration_ms=generalist.telemetry.duration_ms,
            input_tokens=generalist_usage.input_tokens,
            output_tokens=generalist_usage.output_tokens,
            total_tokens=generalist_usage.total_tokens,
            estimated_cost_usd=generalist.estimated_cost_usd,
            decision=generalist.result.decision,
            confidence=generalist.result.confidence,
        ),
        human_review=None,
    )


def build_context_identity(context: ContextBundle) -> EvaluationContextIdentity:
    serialized_context = json.dumps(
        context.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return EvaluationContextIdentity(
        repository_path=context.repository_path,
        commit_sha=context.commit_sha,
        branch=context.branch,
        working_tree_dirty=context.working_tree_dirty,
        evidence_manifest=[
            ContextEvidenceIdentity(
                id=item.id,
                file_path=item.file_path,
                truncated=item.truncated,
            )
            for item in context.evidence
        ],
        context_sha256=_text_sha256(serialized_context),
    )


def prompt_for_comparison_review(
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    now_fn: Callable[[], datetime] | None = None,
) -> HumanComparisonReview:
    generalist_scores = _prompt_for_scores(
        "Generalist",
        input_fn,
        print_fn,
    )
    council_scores = _prompt_for_scores(
        "Council",
        input_fn,
        print_fn,
    )

    while True:
        raw_preference = input_fn(
            "Which result improved the planning conversation more? "
            "[A] Generalist  [B] Council  [T] Tie: "
        ).strip().lower()
        preference_by_choice = {
            "a": ComparisonPreference.GENERALIST,
            "generalist": ComparisonPreference.GENERALIST,
            "b": ComparisonPreference.COUNCIL,
            "council": ComparisonPreference.COUNCIL,
            "t": ComparisonPreference.TIE,
            "tie": ComparisonPreference.TIE,
        }
        preference = preference_by_choice.get(raw_preference)
        if preference is not None:
            break
        print_fn("Enter A, B, or T.")

    while True:
        reason = input_fn("Why? ").strip()
        if reason:
            break
        print_fn("A comparison reason is required.")

    return HumanComparisonReview(
        generalist_scores=generalist_scores,
        council_scores=council_scores,
        preference=preference,
        reason=reason,
        insights=[],
        timestamp=(now_fn or _utc_now)(),
    )


def _prompt_for_scores(
    path_name: str,
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> ComparisonRubricScores:
    print_fn(f"Score {path_name} from 1 to 5 for each dimension.")
    scores: dict[str, int] = {}

    for field_name, label in RUBRIC_DIMENSIONS:
        while True:
            raw_score = input_fn(f"{path_name} - {label}: ").strip()
            try:
                score = int(raw_score)
            except ValueError:
                print_fn("Enter an integer from 1 to 5.")
                continue
            if 1 <= score <= 5:
                scores[field_name] = score
                break
            print_fn("Enter an integer from 1 to 5.")

    return ComparisonRubricScores(**scores)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
