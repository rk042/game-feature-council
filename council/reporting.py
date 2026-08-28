import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from council.models import (
    ComparisonRecord,
    ContextBundle,
    CouncilExecution,
    DirectorDecision,
    DirectorResult,
    GeneralistExecution,
    HumanAction,
    HumanComparisonReview,
    HumanDecision,
    RunRecord,
)
from council.pricing import (
    DEFAULT_PRICING_SNAPSHOT,
    PricingSnapshot,
    estimate_cost,
)


EXPECTED_ARTIFACT_FILES = frozenset(
    {
        "input.json",
        "context.json",
        "game_design.json",
        "technical.json",
        "analytics.json",
        "scope_risk.json",
        "producer.json",
        "director.json",
        "run.json",
        "report.md",
    }
)
EXPECTED_EVALUATION_ARTIFACT_FILES = EXPECTED_ARTIFACT_FILES | {
    "generalist.json",
    "comparison.json",
}
EXPECTED_GENERALIST_ARTIFACT_FILES = frozenset(
    {"input.json", "context.json", "generalist.json", "report.md"}
)
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_ROLE_ORDER = (
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
    "producer",
    "director",
)


class RunArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewArtifacts:
    run_directory: Path
    run_record: RunRecord | None
    comparison: ComparisonRecord | None
    generalist: GeneralistExecution | None


def create_run_record(
    feature_input: str,
    execution: CouncilExecution,
    *,
    run_id: str | None = None,
    pricing: PricingSnapshot = DEFAULT_PRICING_SNAPSHOT,
) -> RunRecord:
    context = execution.result.context
    cost = estimate_cost(execution.telemetry.roles, pricing)
    return RunRecord(
        run_id=run_id or str(uuid4()),
        feature_input=feature_input,
        repository_path=context.repository_path,
        repository_commit_sha=context.commit_sha,
        repository_branch=context.branch,
        working_tree_dirty=context.working_tree_dirty,
        telemetry=execution.telemetry,
        estimated_cost_usd=cost.estimated_cost_usd,
        unpriced_models=cost.unpriced_models,
        pricing_snapshot_id=pricing.identifier,
        council_result=execution.result,
        ai_recommendation=execution.result.director.decision,
        human_decision=None,
    )


def write_run_artifacts(
    record: RunRecord,
    output_root: str | Path,
) -> Path:
    run_directory = _create_run_directory(
        output_root,
        record.run_id,
        record.repository_path,
    )

    result = record.council_result
    context = result.context
    input_payload = {
        "run_id": record.run_id,
        "feature_input": record.feature_input,
        "repository_path": record.repository_path,
        "repository_commit_sha": record.repository_commit_sha,
        "repository_branch": record.repository_branch,
        "working_tree_dirty": record.working_tree_dirty,
        "started_at": record.telemetry.started_at.isoformat(),
    }

    _write_json(run_directory / "input.json", input_payload)
    _write_json(run_directory / "context.json", context.model_dump(mode="json"))
    _write_json(
        run_directory / "game_design.json",
        result.game_design.model_dump(mode="json"),
    )
    _write_json(
        run_directory / "technical.json",
        result.technical.model_dump(mode="json"),
    )
    _write_json(
        run_directory / "analytics.json",
        result.analytics.model_dump(mode="json"),
    )
    _write_json(
        run_directory / "scope_risk.json",
        result.scope_risk.model_dump(mode="json"),
    )
    _write_json(
        run_directory / "producer.json",
        result.producer.model_dump(mode="json"),
    )
    _write_json(
        run_directory / "director.json",
        result.director.model_dump(mode="json"),
    )
    _write_json(
        run_directory / "run.json",
        record.model_dump(mode="json"),
    )
    (run_directory / "report.md").write_text(
        render_markdown_report(record),
        encoding="utf-8",
    )
    return run_directory


def write_generalist_artifacts(
    run_id: str,
    feature_input: str,
    context: ContextBundle,
    execution: GeneralistExecution,
    output_root: str | Path,
    *,
    started_at: datetime,
) -> Path:
    run_directory = _create_run_directory(
        output_root,
        run_id,
        context.repository_path,
    )
    input_payload = {
        "run_id": run_id,
        "mode": "generalist",
        "feature_input": feature_input,
        "repository_path": context.repository_path,
        "repository_commit_sha": context.commit_sha,
        "repository_branch": context.branch,
        "working_tree_dirty": context.working_tree_dirty,
        "started_at": started_at.isoformat(),
    }

    _write_json(run_directory / "input.json", input_payload)
    _write_json(
        run_directory / "context.json",
        context.model_dump(mode="json"),
    )
    _write_json(
        run_directory / "generalist.json",
        execution.model_dump(mode="json"),
    )
    (run_directory / "report.md").write_text(
        _render_generalist_report(run_id, feature_input, context, execution),
        encoding="utf-8",
    )
    return run_directory


def prompt_for_human_decision(
    director: DirectorResult,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    now_fn: Callable[[], datetime] | None = None,
) -> HumanDecision:
    action: HumanAction
    final_decision: DirectorDecision | None

    while True:
        choice = input_fn("[A] Accept  [R] Reject  [M] Modify: ").strip().lower()
        if choice in {"a", "accept"}:
            action = HumanAction.ACCEPT
            final_decision = director.decision
            break
        if choice in {"r", "reject"}:
            action = HumanAction.REJECT
            final_decision = None
            break
        if choice in {"m", "modify"}:
            action = HumanAction.MODIFY
            final_decision = _prompt_for_modified_decision(
                director.decision,
                input_fn,
                print_fn,
            )
            break
        print_fn("Enter A, R, or M.")

    note = input_fn("Optional note (press Enter to skip): ").strip() or None
    return HumanDecision(
        action=action,
        final_decision=final_decision,
        note=note,
        timestamp=(now_fn or _utc_now)(),
    )


def update_run_with_human_decision(
    run_directory: str | Path,
    human_decision: HumanDecision,
) -> RunRecord:
    directory = Path(run_directory).expanduser().resolve()
    record = _read_run_record(directory)
    _validate_human_decision(human_decision, record.ai_recommendation)
    if record.human_decision is not None:
        if record.human_decision == human_decision:
            return record
        raise RunArtifactError(
            "Product/Director human review is already completed."
        )

    run_path = _existing_artifact_path(directory, "run.json")
    report_path = _existing_artifact_path(directory, "report.md")
    comparison = _read_optional_comparison(directory)
    if comparison is not None:
        if comparison.council_run_id != record.run_id:
            raise RunArtifactError(
                "Comparison record does not belong to this council run."
            )
        _validate_comparison_identity(record, comparison)

    updated_record = record.model_copy(
        update={"human_decision": human_decision}
    )
    _write_json(run_path, updated_record.model_dump(mode="json"))
    report_path.write_text(
        render_markdown_report(
            updated_record,
            comparison,
        ),
        encoding="utf-8",
    )
    return updated_record


def write_evaluation_artifacts(
    run_directory: str | Path,
    comparison: ComparisonRecord,
) -> None:
    directory = Path(run_directory).expanduser().resolve()
    record = _read_run_record(directory)
    if comparison.council_run_id != record.run_id:
        raise RunArtifactError(
            "Comparison record does not belong to this council run."
        )

    generalist_path = directory / "generalist.json"
    comparison_path = directory / "comparison.json"
    if generalist_path.exists() or comparison_path.exists():
        raise RunArtifactError(
            "Evaluation artifacts already exist for this council run."
        )

    _write_json(
        generalist_path,
        comparison.generalist.model_dump(mode="json"),
    )
    _write_json(
        comparison_path,
        comparison.model_dump(mode="json"),
    )
    (directory / "report.md").write_text(
        render_markdown_report(record, comparison),
        encoding="utf-8",
    )


def update_comparison_with_human_review(
    run_directory: str | Path,
    human_review: HumanComparisonReview,
) -> ComparisonRecord:
    directory = Path(run_directory).expanduser().resolve()
    record = _read_run_record(directory)
    comparison_path = _existing_artifact_path(directory, "comparison.json")
    report_path = _existing_artifact_path(directory, "report.md")

    try:
        comparison = ComparisonRecord.model_validate_json(
            comparison_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise RunArtifactError(
            f"Unable to read comparison record from: {comparison_path}"
        ) from error
    if comparison.council_run_id != record.run_id:
        raise RunArtifactError(
            "Comparison record does not belong to this council run."
        )
    _validate_comparison_identity(record, comparison)
    if comparison.human_review is not None:
        if comparison.human_review == human_review:
            return comparison
        raise RunArtifactError(
            "Council-vs-Generalist human review is already completed."
        )

    updated = comparison.model_copy(update={"human_review": human_review})
    _write_json(comparison_path, updated.model_dump(mode="json"))
    report_path.write_text(
        render_markdown_report(record, updated),
        encoding="utf-8",
    )
    return updated


def render_markdown_report(
    record: RunRecord,
    comparison: ComparisonRecord | None = None,
) -> str:
    result = record.council_result
    director = result.director
    producer = result.producer
    experiment = director.experiment
    context = result.context

    report = [
        "# Game Feature Council Report",
        "",
        "## Run",
        "",
        f"- Run ID: `{record.run_id}`",
        f"- Started: {record.telemetry.started_at.isoformat()}",
        "",
        "## Repository",
        "",
        f"- Path: `{record.repository_path}`",
        f"- Commit: `{record.repository_commit_sha}`",
        f"- Branch: `{record.repository_branch or 'detached HEAD'}`",
        f"- Tracked working tree dirty: {record.working_tree_dirty}",
        "",
        "## Feature",
        "",
        record.feature_input,
        "",
        "## Director Recommendation",
        "",
        f"- Decision: **{director.decision.value}**",
        f"- Confidence: {director.confidence.value}",
        f"- Confidence reason: {director.confidence_reason}",
        "",
        "## Proposed Experiment",
        "",
        f"- Player problem: {experiment.player_problem}",
        f"- Hypothesis: {experiment.hypothesis}",
        f"- Riskiest assumption: {experiment.riskiest_assumption}",
        f"- Smallest experiment: {experiment.smallest_experiment}",
        "",
        "### Build",
        "",
        *_markdown_bullets(experiment.build),
        "",
        "### Do Not Build",
        "",
        *_markdown_bullets(experiment.do_not_build),
        "",
        "## Effort",
        "",
        f"- Range: {_format_effort_range(director.effort.developer_days_min, director.effort.developer_days_max)}",
        f"- Confidence: {director.effort.confidence.value}",
        f"- Basis: {director.effort.basis}",
        "",
        "## Key Agreements",
        "",
        *_markdown_bullets(producer.agreements),
        "",
        "## Key Disagreements",
        "",
        *_markdown_bullets(producer.disagreements),
        "",
        "## Risks / Unknowns",
        "",
        *_markdown_bullets(_risks_and_unknowns(record)),
        "",
        _director_questions_heading(record),
        "",
        *_markdown_bullets(_director_question_lines(record)),
        "",
        "## Decision Conditions",
        "",
        "### Scale",
        "",
        *_markdown_bullets(director.decision_conditions.scale),
        "",
        "### Iterate",
        "",
        *_markdown_bullets(director.decision_conditions.iterate),
        "",
        "### Kill",
        "",
        *_markdown_bullets(director.decision_conditions.kill),
        "",
        "### Need More Data",
        "",
        *_markdown_bullets(director.decision_conditions.need_more_data),
        "",
        "## Evidence / Grounding",
        "",
        f"- Repository SHA: `{context.commit_sha}`",
        f"- Tracked working tree dirty: {context.working_tree_dirty}",
        *_markdown_bullets(
            f"`{item.id}` — `{item.file_path}`"
            for item in context.evidence
        ),
        "",
        "## Runtime",
        "",
        f"- Total duration: {record.telemetry.total_duration_ms:.3f} ms",
        f"- Input tokens: {record.telemetry.total_usage.input_tokens}",
        f"- Output tokens: {record.telemetry.total_usage.output_tokens}",
        f"- Total tokens: {record.telemetry.total_usage.total_tokens}",
        f"- Estimated cost USD: {_format_cost(record)}",
        f"- Pricing snapshot: `{record.pricing_snapshot_id}`",
        "",
        *_runtime_role_lines(record),
        "",
        *_comparison_report_lines(comparison),
        "## Human Decision",
        "",
        *_human_decision_lines(record),
        "",
    ]
    return "\n".join(report)


def _prompt_for_modified_decision(
    ai_recommendation: DirectorDecision,
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> DirectorDecision:
    allowed_values = ", ".join(decision.value for decision in DirectorDecision)
    print_fn(f"Allowed decisions: {allowed_values}")

    while True:
        raw_decision = input_fn("Final decision: ").strip().upper()
        try:
            final_decision = DirectorDecision(raw_decision)
        except ValueError:
            print_fn("Enter one of the allowed DirectorDecision values.")
            continue
        if final_decision == ai_recommendation:
            print_fn("Modify must choose a decision different from the AI recommendation.")
            continue
        return final_decision


def _validate_human_decision(
    decision: HumanDecision,
    ai_recommendation: DirectorDecision,
) -> None:
    if (
        decision.action == HumanAction.ACCEPT
        and decision.final_decision != ai_recommendation
    ):
        raise RunArtifactError(
            "An accepted human decision must preserve the AI recommendation."
        )
    if decision.action == HumanAction.MODIFY:
        if decision.final_decision is None:
            raise RunArtifactError("A modified decision requires a final decision.")
        if decision.final_decision == ai_recommendation:
            raise RunArtifactError(
                "A modified decision must differ from the AI recommendation."
            )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _create_run_directory(
    output_root: str | Path,
    run_id: str,
    repository_path: str,
) -> Path:
    root = Path(output_root).expanduser().resolve()
    if not root.is_dir():
        raise RunArtifactError(
            f"Output root is not an existing directory: {root}"
        )
    _validate_safe_run_id(run_id)

    target_repository = Path(repository_path).expanduser().resolve()
    if _is_within(root, target_repository):
        raise RunArtifactError(
            "Run artifacts cannot be written inside the target repository."
        )

    run_directory = (root / run_id).resolve()
    if not _is_within(run_directory, root):
        raise RunArtifactError("Resolved run directory escapes the output root.")

    try:
        run_directory.mkdir()
    except FileExistsError as error:
        raise RunArtifactError(
            f"Run directory already exists: {run_directory}"
        ) from error
    return run_directory


def _render_generalist_report(
    run_id: str,
    feature_input: str,
    context: ContextBundle,
    execution: GeneralistExecution,
) -> str:
    result = execution.result
    usage = execution.telemetry.usage
    cost = (
        str(execution.estimated_cost_usd)
        if execution.estimated_cost_usd is not None
        else "Unavailable"
    )
    return "\n".join(
        [
            "# Generalist Baseline Report",
            "",
            f"- Run ID: `{run_id}`",
            f"- Repository: `{context.repository_path}`",
            f"- Commit: `{context.commit_sha}`",
            f"- Tracked working tree dirty: {context.working_tree_dirty}",
            "",
            "## Feature",
            "",
            feature_input,
            "",
            "## Recommendation",
            "",
            f"- Decision: **{result.decision.value}**",
            f"- Confidence: {result.confidence.value}",
            f"- Confidence reason: {result.confidence_reason}",
            "",
            "## Runtime",
            "",
            f"- Model: `{execution.telemetry.model}`",
            f"- Duration: {execution.telemetry.duration_ms:.3f} ms",
            f"- Input tokens: {usage.input_tokens}",
            f"- Output tokens: {usage.output_tokens}",
            f"- Total tokens: {usage.total_tokens}",
            f"- Estimated cost USD: {cost}",
            f"- Pricing snapshot: `{execution.pricing_snapshot_id}`",
            "",
            "## Human Decision",
            "",
            "- Status: Pending",
            "",
        ]
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _markdown_bullets(values: Iterable[str]) -> list[str]:
    items = [f"- {value}" for value in values]
    return items or ["- None"]


def _format_effort_range(
    minimum: float | None,
    maximum: float | None,
) -> str:
    if minimum is None and maximum is None:
        return "Unavailable"
    return f"{minimum if minimum is not None else '?'}–{maximum if maximum is not None else '?'} developer days"


def _risks_and_unknowns(record: RunRecord) -> list[str]:
    result = record.council_result
    values: list[str] = []
    retained: list[str] = []

    def retain(label: str, value: str) -> None:
        if any(_obvious_report_duplicate(value, prior) for prior in retained):
            return
        retained.append(value)
        values.append(f"{label}: {value}")

    # Producer unknowns are consolidated and therefore take precedence over
    # overlapping specialist wording in the human-facing report.
    for unknown in result.producer.unknowns:
        retain("Unknown", unknown)

    for specialist in (
        result.game_design,
        result.technical,
        result.analytics,
        result.scope_risk,
    ):
        for risk in specialist.risks:
            retain("Risk", risk)
        for unknown in specialist.unknowns:
            retain("Unknown", unknown)
    return values


def _obvious_report_duplicate(candidate: str, retained: str) -> bool:
    return _normalized_report_words(candidate) == _normalized_report_words(
        retained
    )


def _normalized_report_words(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters = [
        " "
        if character.isspace()
        or unicodedata.category(character).startswith("P")
        else character
        for character in normalized
    ]
    collapsed = " ".join("".join(characters).split())
    if collapsed:
        return collapsed

    # Punctuation-only values need a non-empty identity so unrelated entries
    # are not all treated as the same duplicate.
    return " ".join(normalized.split())


def _director_questions_heading(record: RunRecord) -> str:
    if record.human_decision is None:
        return "## Human Decisions Required"
    return "## Director Questions — Resolved by Human Decision"


def _director_question_lines(record: RunRecord) -> list[str]:
    questions = record.council_result.director.human_decisions_required
    if record.human_decision is None:
        return questions
    return [f"Resolved: {question}" for question in questions]


def _runtime_role_lines(record: RunRecord) -> list[str]:
    lines: list[str] = []
    for role in _ROLE_ORDER:
        telemetry = record.telemetry.roles.get(role)
        if telemetry is None:
            continue
        lines.append(
            f"- {role}: model `{telemetry.model}`, "
            f"{telemetry.duration_ms:.3f} ms, "
            f"{telemetry.usage.input_tokens} input / "
            f"{telemetry.usage.output_tokens} output tokens"
        )
    return lines or ["- No role telemetry available"]


def _format_cost(record: RunRecord) -> str:
    if record.estimated_cost_usd is not None:
        return str(record.estimated_cost_usd)
    models = ", ".join(record.unpriced_models) or "unknown"
    return f"Unavailable (unpriced models: {models})"


def _human_decision_lines(record: RunRecord) -> list[str]:
    decision = record.human_decision
    if decision is None:
        return ["- Status: Pending"]
    return [
        f"- Status: {decision.action.value}",
        f"- Final decision: {decision.final_decision.value if decision.final_decision else 'None'}",
        f"- Note: {decision.note or 'None'}",
        f"- Timestamp: {decision.timestamp.isoformat()}",
    ]


def _read_run_record(directory: Path) -> RunRecord:
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise RunArtifactError(f"Not a council run directory: {directory}")
    run_path = _existing_artifact_path(directory, "run.json")

    try:
        record = RunRecord.model_validate_json(
            run_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise RunArtifactError(
            f"Unable to read run record from: {run_path}"
        ) from error
    if directory.name != record.run_id:
        raise RunArtifactError(
            "Run directory name does not match the persisted run ID."
        )
    if record.ai_recommendation != record.council_result.director.decision:
        raise RunArtifactError(
            "Persisted AI recommendation does not match the Director result."
        )
    context = record.council_result.context
    if (
        record.feature_input != context.feature_input
        or record.repository_path != context.repository_path
        or record.repository_commit_sha != context.commit_sha
        or record.repository_branch != context.branch
        or record.working_tree_dirty != context.working_tree_dirty
    ):
        raise RunArtifactError(
            "Persisted run identity does not match the Council ContextBundle."
        )
    if record.human_decision is not None:
        _validate_human_decision(
            record.human_decision,
            record.ai_recommendation,
        )

    target_repository = Path(record.repository_path).expanduser().resolve()
    if _is_within(directory, target_repository):
        raise RunArtifactError(
            "Run artifacts cannot be updated inside the target repository."
        )
    return record


def _read_optional_comparison(directory: Path) -> ComparisonRecord | None:
    comparison_path = directory / "comparison.json"
    if not comparison_path.exists() and not comparison_path.is_symlink():
        return None
    comparison_path = _existing_artifact_path(directory, "comparison.json")
    try:
        return ComparisonRecord.model_validate_json(
            comparison_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise RunArtifactError(
            f"Unable to read comparison record from: {comparison_path}"
        ) from error


def load_review_artifacts(
    output_root: str | Path,
    run_id: str,
) -> ReviewArtifacts:
    directory = _resolve_existing_run_directory(output_root, run_id)
    run_path = directory / "run.json"

    if run_path.exists() or run_path.is_symlink():
        record = _read_run_record(directory)
        comparison = _read_optional_comparison(directory)
        if comparison is not None and comparison.council_run_id != record.run_id:
            raise RunArtifactError(
                "Comparison record does not belong to this council run."
            )
        if comparison is not None:
            _validate_comparison_identity(record, comparison)
        _existing_artifact_path(directory, "report.md")
        return ReviewArtifacts(
            run_directory=directory,
            run_record=record,
            comparison=comparison,
            generalist=None,
        )

    comparison_path = directory / "comparison.json"
    if comparison_path.exists() or comparison_path.is_symlink():
        raise RunArtifactError(
            "Generalist-only run cannot contain a Council comparison artifact."
        )

    input_path = directory / "input.json"
    if not input_path.exists() and not input_path.is_symlink():
        raise RunArtifactError(
            f"Required artifact does not exist: {directory / 'run.json'}"
        )

    generalist = _read_generalist_only_execution(directory, run_id)
    return ReviewArtifacts(
        run_directory=directory,
        run_record=None,
        comparison=None,
        generalist=generalist,
    )


def _resolve_existing_run_directory(
    output_root: str | Path,
    run_id: str,
) -> Path:
    _validate_safe_run_id(run_id)
    root = Path(output_root).expanduser().resolve()
    if not root.is_dir():
        raise RunArtifactError(
            f"Run output root is not an existing directory: {root}"
        )

    unresolved_directory = root / run_id
    if unresolved_directory.is_symlink():
        raise RunArtifactError(
            f"Run directory cannot be a symbolic link: {unresolved_directory}"
        )
    directory = unresolved_directory.resolve()
    if not _is_within(directory, root):
        raise RunArtifactError("Resolved run directory escapes the output root.")
    if not directory.is_dir():
        raise RunArtifactError(f"Run was not found: {run_id}")
    if directory.name != run_id:
        raise RunArtifactError("Resolved run directory does not match the run ID.")
    return directory


def _read_generalist_only_execution(
    directory: Path,
    run_id: str,
) -> GeneralistExecution:
    input_path = _existing_artifact_path(directory, "input.json")
    context_path = _existing_artifact_path(directory, "context.json")
    generalist_path = _existing_artifact_path(directory, "generalist.json")
    _existing_artifact_path(directory, "report.md")

    try:
        input_payload = json.loads(input_path.read_text(encoding="utf-8"))
        context = ContextBundle.model_validate_json(
            context_path.read_text(encoding="utf-8")
        )
        generalist = GeneralistExecution.model_validate_json(
            generalist_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise RunArtifactError(
            f"Unable to read Generalist-only run artifacts from: {directory}"
        ) from error

    if not isinstance(input_payload, dict):
        raise RunArtifactError("Generalist input artifact must contain an object.")
    if input_payload.get("mode") != "generalist":
        raise RunArtifactError(
            "Missing run.json is valid only for a Generalist-only run."
        )
    if input_payload.get("run_id") != run_id:
        raise RunArtifactError(
            "Generalist input run ID does not match the run directory."
        )
    feature_input = input_payload.get("feature_input")
    if not isinstance(feature_input, str):
        raise RunArtifactError("Generalist input feature is missing or invalid.")
    feature_sha256 = hashlib.sha256(feature_input.encode("utf-8")).hexdigest()
    if generalist.feature_sha256 != feature_sha256:
        raise RunArtifactError(
            "Generalist feature identity does not match the input artifact."
        )

    from council.evaluation import build_context_identity

    if generalist.context_identity != build_context_identity(context):
        raise RunArtifactError(
            "Generalist context identity does not match the context artifact."
        )
    target_repository = Path(context.repository_path).expanduser().resolve()
    if _is_within(directory, target_repository):
        raise RunArtifactError(
            "Run artifacts cannot be reviewed inside the target repository."
        )
    return generalist


def _validate_comparison_identity(
    record: RunRecord,
    comparison: ComparisonRecord,
) -> None:
    from council.evaluation import EvaluationError, create_comparison_record

    try:
        expected = create_comparison_record(record, comparison.generalist)
    except EvaluationError as error:
        raise RunArtifactError(
            "Comparison feature or context identity does not match the Council run."
        ) from error
    actual_without_review = comparison.model_copy(update={"human_review": None})
    if actual_without_review != expected:
        raise RunArtifactError(
            "Comparison metrics or identity do not match the Council run."
        )


def _validate_safe_run_id(run_id: str) -> None:
    reserved_stem = run_id.split(".", 1)[0].upper()
    if (
        _SAFE_RUN_ID.fullmatch(run_id) is None
        or run_id.endswith(".")
        or reserved_stem in _WINDOWS_RESERVED_NAMES
    ):
        raise RunArtifactError(f"Unsafe run ID: {run_id!r}")


def _existing_artifact_path(directory: Path, filename: str) -> Path:
    directory = Path(directory).expanduser().resolve()
    path = directory / filename
    if path.is_symlink():
        raise RunArtifactError(f"Artifact cannot be a symbolic link: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RunArtifactError(f"Required artifact does not exist: {path}") from error
    if not _is_within(resolved, directory) or not resolved.is_file():
        raise RunArtifactError(f"Unsafe or invalid artifact path: {path}")
    return resolved


def _comparison_report_lines(
    comparison: ComparisonRecord | None,
) -> list[str]:
    if comparison is None:
        return []

    generalist = comparison.generalist_metrics
    council = comparison.council_metrics
    lines = [
        "## Generalist Baseline Comparison",
        "",
        "| Metric | Generalist | Council |",
        "| --- | ---: | ---: |",
        f"| Duration (ms) | {generalist.duration_ms:.3f} | {council.duration_ms:.3f} |",
        f"| Input tokens | {generalist.input_tokens} | {council.input_tokens} |",
        f"| Output tokens | {generalist.output_tokens} | {council.output_tokens} |",
        f"| Total tokens | {generalist.total_tokens} | {council.total_tokens} |",
        f"| Estimated cost USD | {_comparison_cost(generalist.estimated_cost_usd)} | {_comparison_cost(council.estimated_cost_usd)} |",
        f"| Decision | {generalist.decision.value} | {council.decision.value} |",
        f"| Confidence | {generalist.confidence.value} | {council.confidence.value} |",
        "",
    ]
    review = comparison.human_review
    if review is None:
        lines.extend(["- Human comparison review: Pending", ""])
        return lines

    lines.extend(
        [
            f"- Human preference: **{review.preference.value}**",
            f"- Reason: {review.reason}",
            "",
            "### Human Rubric",
            "",
            "| Dimension | Generalist | Council |",
            "| --- | ---: | ---: |",
        ]
    )
    for dimension in type(review.generalist_scores).model_fields:
        label = dimension.replace("_", " ").title()
        lines.append(
            f"| {label} | "
            f"{getattr(review.generalist_scores, dimension)} | "
            f"{getattr(review.council_scores, dimension)} |"
        )
    if review.insights:
        lines.extend(["", "### Human Insight Comparisons", ""])
        for insight in review.insights:
            lines.extend(
                [
                    f"- **{insight.issue}**",
                    f"  - Generalist: {insight.generalist_observation}",
                    f"  - Council: {insight.council_observation}",
                    f"  - Assessment: {insight.assessment}",
                ]
            )
    lines.append("")
    return lines


def _comparison_cost(value: object) -> str:
    return "Unavailable" if value is None else str(value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
