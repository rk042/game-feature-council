import asyncio
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import Protocol
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typer.main import get_command

from council.evaluation import prompt_for_comparison_review
from council.models import (
    ContextBundle,
    CouncilExecution,
    GeneralistExecution,
    RoleTelemetry,
    TokenUsage,
)
from council.pricing import DEFAULT_PRICING_SNAPSHOT, estimate_cost
from council.reporting import (
    ReviewArtifacts,
    RunArtifactError,
    load_review_artifacts,
    prompt_for_human_decision,
    update_comparison_with_human_review,
    update_run_with_human_decision,
)


MODE_COUNCIL = "council"
MODE_GENERALIST = "generalist"
MODE_BOTH = "both"
MODES = (MODE_COUNCIL, MODE_GENERALIST, MODE_BOTH)
SPECIALIST_ROLES = (
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
)
OUTPUT_TOKEN_ALLOWANCES = {
    "game_design": 1_400,
    "technical": 1_400,
    "analytics": 1_400,
    "scope_risk": 1_400,
    "producer": 1_800,
    "director": 1_800,
    "generalist": 2_000,
}
CHARS_PER_TOKEN = 4
CONSERVATIVE_TOKEN_MULTIPLIER = 2
REDACTION_MARKER = "[REDACTED]"


app = typer.Typer(
    name="council",
    help="Evaluate game-feature experiments against a Git repository.",
    no_args_is_help=True,
    add_completion=False,
)


class RunMode(str, Enum):
    COUNCIL = MODE_COUNCIL
    GENERALIST = MODE_GENERALIST
    BOTH = MODE_BOTH


class CliError(RuntimeError):
    def __init__(self, message: str, *, before_api_calls: bool) -> None:
        super().__init__(message)
        self.before_api_calls = before_api_calls


@dataclass(frozen=True)
class _RoleBudget:
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class PreflightEstimate:
    expected_calls: int
    rough_input_tokens: int
    output_token_allowance: int
    estimated_cost_low_usd: Decimal | None
    estimated_cost_high_usd: Decimal | None
    conservative_max_cost_usd: Decimal | None
    unpriced_models: tuple[str, ...]


@dataclass(frozen=True)
class RunArguments:
    repo: str
    feature: str | None
    feature_file: str | None
    mode: str
    max_cost_usd: Decimal | None
    dry_run: bool
    yes: bool
    output_dir: str


@dataclass(frozen=True)
class ReviewArguments:
    run: str
    output_dir: str


class TerminalOutput(Protocol):
    def line(self, text: str) -> None: ...

    def heading(self, text: str) -> None: ...

    def preflight(
        self,
        context: ContextBundle,
        mode: str,
        specialist_model: str | None,
        synthesis_model: str,
        estimate: PreflightEstimate,
    ) -> None: ...

    def warning(self, text: str) -> None: ...

    def error(self, text: str) -> None: ...

    def success(self, text: str) -> None: ...


class PlainOutput:
    def __init__(self, print_fn: Callable[[str], None]) -> None:
        self._print = print_fn

    def line(self, text: str) -> None:
        self._print(text)

    def heading(self, text: str) -> None:
        self._print(text)

    def preflight(
        self,
        context: ContextBundle,
        mode: str,
        specialist_model: str | None,
        synthesis_model: str,
        estimate: PreflightEstimate,
    ) -> None:
        self._print("Preflight estimate (rough; actual telemetry is authoritative)")
        for label, value in _preflight_rows(
            context,
            mode,
            specialist_model,
            synthesis_model,
            estimate,
        ):
            self._print(f"{label}: {value}")

    def warning(self, text: str) -> None:
        self._print(text)

    def error(self, text: str) -> None:
        self._print(f"Error: {text}")

    def success(self, text: str) -> None:
        self._print(text)


class RichOutput:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._error_console = Console(stderr=True)

    def line(self, text: str) -> None:
        self._console.print(text, markup=False)

    def heading(self, text: str) -> None:
        self._console.print(f"[bold cyan]{text}[/bold cyan]")

    def preflight(
        self,
        context: ContextBundle,
        mode: str,
        specialist_model: str | None,
        synthesis_model: str,
        estimate: PreflightEstimate,
    ) -> None:
        table = Table(
            title="Preflight estimate",
            caption="Rough estimate; completed-run telemetry is authoritative.",
        )
        table.add_column("Item", style="bold")
        table.add_column("Value")
        for label, value in _preflight_rows(
            context,
            mode,
            specialist_model,
            synthesis_model,
            estimate,
        ):
            table.add_row(label, value)
        self._console.print(table)

    def warning(self, text: str) -> None:
        self._console.print(
            Panel(Text(text), title="Notice", border_style="yellow")
        )

    def error(self, text: str) -> None:
        self._error_console.print(
            Panel(Text(text), title="Error", border_style="red")
        )

    def success(self, text: str) -> None:
        self._console.print(f"[bold green]{text}[/bold green]")


def _validate_cost_option(
    _context: typer.Context,
    _parameter: typer.CallbackParam,
    value: str | None,
) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise typer.BadParameter("must be a non-negative decimal value") from error
    if not parsed.is_finite() or parsed < 0:
        raise typer.BadParameter("must be a non-negative decimal value")
    return parsed


@app.command("run")
def typer_run_command(
    repo: str = typer.Option(..., "--repo", help="Git repository to inspect."),
    feature: str | None = typer.Option(
        None,
        "--feature",
        help="Feature text supplied directly; use exactly one feature source.",
    ),
    feature_file: str | None = typer.Option(
        None,
        "--feature-file",
        help="UTF-8 feature text file; use exactly one feature source.",
    ),
    mode: RunMode = typer.Option(
        RunMode.BOTH,
        "--mode",
        help="Evaluation path: council, generalist, or both.",
    ),
    max_cost_usd: str | None = typer.Option(
        None,
        "--max-cost-usd",
        callback=_validate_cost_option,
        help="Conservative preflight cost ceiling in USD.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Build context and show preflight without API calls or artifacts.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Explicitly consent without the interactive prompt.",
    ),
    output_dir: str = typer.Option(
        "runs",
        "--output-dir",
        help="Artifact root directory (default: runs).",
    ),
) -> None:
    output = RichOutput()
    output.heading("Game Feature Council")
    arguments = RunArguments(
        repo=repo,
        feature=feature,
        feature_file=feature_file,
        mode=mode.value,
        max_cost_usd=max_cost_usd,
        dry_run=dry_run,
        yes=yes,
        output_dir=output_dir,
    )
    _exit_for_code(
        _run_with_error_boundary(
            "run",
            lambda: asyncio.run(
                _run_command(arguments, input_fn=input, output=output)
            ),
            output,
        )
    )


@app.command("review")
def typer_review_command(
    run: str = typer.Option(..., "--run", help="Existing run identifier."),
    output_dir: str = typer.Option(
        "runs",
        "--output-dir",
        help="Artifact root directory containing the run (default: runs).",
    ),
) -> None:
    output = RichOutput()
    output.heading("Game Feature Council Review")
    arguments = ReviewArguments(run=run, output_dir=output_dir)
    _exit_for_code(
        _run_with_error_boundary(
            "review",
            lambda: _review_command(
                arguments,
                input_fn=input,
                print_fn=output.line,
            ),
            output,
        )
    )


def _exit_for_code(code: int) -> None:
    if code:
        raise typer.Exit(code)


def console_main() -> None:
    """Console-script and ``python -m council`` entry point."""
    app()


def parse_args(argv: Sequence[str] | None = None) -> SimpleNamespace:
    """Compatibility helper backed by the Typer command tree."""
    command = get_command(app)
    context = command.make_context(
        "council",
        [],
        resilient_parsing=True,
    )
    command_name, subcommand, remaining = command.resolve_command(
        context,
        list(argv or ()),
    )
    subcontext = subcommand.make_context(
        command_name,
        remaining,
        parent=context,
    )
    values = dict(subcontext.params)
    if isinstance(values.get("mode"), RunMode):
        values["mode"] = values["mode"].value
    return SimpleNamespace(command=command_name, **values)


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> int:
    args = parse_args(argv)
    output = PlainOutput(print_fn)
    if args.command == "review":
        return _run_with_error_boundary(
            "review",
            lambda: _review_command(
                ReviewArguments(args.run, args.output_dir),
                input_fn=input_fn,
                print_fn=output.line,
            ),
            output,
        )
    return _run_with_error_boundary(
        "run",
        lambda: asyncio.run(
            _run_command(
                RunArguments(
                    repo=args.repo,
                    feature=args.feature,
                    feature_file=args.feature_file,
                    mode=args.mode,
                    max_cost_usd=args.max_cost_usd,
                    dry_run=args.dry_run,
                    yes=args.yes,
                    output_dir=args.output_dir,
                ),
                input_fn=input_fn,
                output=output,
            )
        ),
        output,
    )


def _run_with_error_boundary(
    command: str,
    action: Callable[[], int],
    output: TerminalOutput,
) -> int:
    try:
        return action()
    except CliError as error:
        safe_message = sanitize_user_facing_text(
            str(error),
            (os.environ.get("OPENAI_API_KEY", ""),),
        )
        output.error(safe_message)
        if error.before_api_calls:
            output.line("Execution stopped before any API calls.")
        else:
            output.line("Execution stopped; no successful run was completed.")
        return 2
    except ModuleNotFoundError as error:
        if command != "run" or not _is_missing_agents_dependency(error):
            raise
        output.error(
            "The run command requires the 'openai-agents' package. Install "
            "the project run dependencies before model execution."
        )
        output.line("Execution stopped before any API calls.")
        return 2
    except KeyboardInterrupt:
        output.line("Execution interrupted; no successful run was completed.")
        return 130


def sanitize_user_facing_text(
    text: str,
    secrets: Sequence[str],
) -> str:
    sanitized = text
    for secret in secrets:
        if secret and secret.strip():
            sanitized = sanitized.replace(secret, REDACTION_MARKER)
    return sanitized


def _is_missing_agents_dependency(error: ModuleNotFoundError) -> bool:
    missing_name = error.name or ""
    return missing_name == "agents" or missing_name.startswith("agents.")


def build_context(repository_path: str, feature: str) -> ContextBundle:
    from council.context import build_context as build

    return build(repository_path, feature)


async def run_council_with_telemetry(
    feature: str,
    context: ContextBundle,
) -> CouncilExecution:
    from council.orchestrator import run_council_with_telemetry as run

    return await run(feature, context)


async def run_generalist(
    feature: str,
    context: ContextBundle,
) -> GeneralistExecution:
    from council.evaluation import run_generalist as run

    return await run(feature, context)


def render_specialist_input(feature: str, context: ContextBundle) -> str:
    from council.orchestrator import render_specialist_input as render

    return render(feature, context)


def load_prompt(filename: str) -> str:
    from council.agents import load_prompt as load

    return load(filename)


def _review_command(
    args: ReviewArguments,
    *,
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> int:
    try:
        artifacts = load_review_artifacts(args.output_dir, args.run)
    except (RunArtifactError, OSError, ValueError) as error:
        raise CliError(str(error), before_api_calls=True) from error

    _print_review_summary(artifacts, print_fn)
    if artifacts.generalist is not None:
        print_fn(
            "Generalist-only runs do not have a Council Product/Director "
            "human-review contract."
        )
        print_fn("Human review complete")
        print_fn("Product/Director: not available for this run")
        print_fn("Council vs Generalist: not available for this run")
        print_fn("Updated: none")
        return 0

    record = artifacts.run_record
    if record is None:
        raise CliError(
            "Run does not contain a Council or Generalist result.",
            before_api_calls=True,
        )

    comparison = artifacts.comparison
    changed_files: list[Path] = []
    try:
        if record.human_decision is None:
            decision = prompt_for_human_decision(
                record.council_result.director,
                input_fn=input_fn,
                print_fn=print_fn,
            )
            record = update_run_with_human_decision(
                artifacts.run_directory,
                decision,
            )
            changed_files.extend(
                [
                    artifacts.run_directory / "run.json",
                    artifacts.run_directory / "report.md",
                ]
            )

        if comparison is not None and comparison.human_review is None:
            review = prompt_for_comparison_review(
                input_fn=input_fn,
                print_fn=print_fn,
            )
            comparison = update_comparison_with_human_review(
                artifacts.run_directory,
                review,
            )
            changed_files.extend(
                [
                    artifacts.run_directory / "comparison.json",
                    artifacts.run_directory / "report.md",
                ]
            )
    except EOFError as error:
        raise CliError(
            "Human review input ended before review was complete.",
            before_api_calls=True,
        ) from error
    except (RunArtifactError, OSError, ValueError) as error:
        raise CliError(
            f"Human review persistence failed: {error}",
            before_api_calls=True,
        ) from error

    print_fn("Human review complete")
    human_decision = record.human_decision
    product_status = (
        human_decision.action.value if human_decision is not None else "pending"
    )
    print_fn(f"Product/Director: {product_status}")
    if comparison is None:
        print_fn("Council vs Generalist: not available for this run")
    elif comparison.human_review is None:
        print_fn("Council vs Generalist: pending")
    else:
        print_fn(
            "Council vs Generalist: "
            f"{comparison.human_review.preference.value}"
        )

    unique_changed_files = list(dict.fromkeys(changed_files))
    if unique_changed_files:
        print_fn("Updated:")
        for path in unique_changed_files:
            print_fn(str(path))
    else:
        print_fn("Updated: none")
    return 0


def _print_review_summary(
    artifacts: ReviewArtifacts,
    print_fn: Callable[[str], None],
) -> None:
    print_fn(f"Run: {artifacts.run_directory.name}")
    if artifacts.generalist is not None:
        print_fn(
            f"AI recommendation: {artifacts.generalist.result.decision.value}"
        )
        print_fn("Product/Director review: Not available")
        print_fn("Council comparison review: Not available")
        return

    record = artifacts.run_record
    if record is None:
        raise CliError(
            "Run does not contain a reviewable result.",
            before_api_calls=True,
        )
    print_fn(f"AI recommendation: {record.ai_recommendation.value}")
    human_decision = record.human_decision
    if human_decision is None:
        print_fn("Product/Director review: Pending")
    else:
        print_fn("Product/Director review: already completed")
        print_fn(f"Action: {human_decision.action.value}")
        final_decision = (
            human_decision.final_decision.value
            if human_decision.final_decision is not None
            else "None"
        )
        print_fn(f"Final decision: {final_decision}")

    comparison = artifacts.comparison
    if comparison is None:
        print_fn("Council comparison review: Not available")
        print_fn(
            "Council-vs-Generalist comparison: not available for this run"
        )
    elif comparison.human_review is None:
        print_fn("Council comparison review: Pending")
    else:
        print_fn("Council comparison review: already completed")
        print_fn(f"Preference: {comparison.human_review.preference.value}")


async def _run_command(
    args: RunArguments,
    *,
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> int:
    from council.context import ContextBuilderError
    from council.evaluation import EvaluationError
    from council.orchestrator import CouncilOrchestrationError

    feature = _read_feature_source(args.feature, args.feature_file)
    try:
        context = build_context(args.repo, feature)
    except ContextBuilderError as error:
        raise CliError(str(error), before_api_calls=True) from error

    specialist_model, synthesis_model = _read_model_configuration(args.mode)
    preflight = build_preflight_estimate(
        feature,
        context,
        args.mode,
        specialist_model,
        synthesis_model,
    )
    _print_preflight(
        context,
        args.mode,
        specialist_model,
        synthesis_model,
        preflight,
        output,
    )
    _enforce_cost_cap(preflight, args.max_cost_usd)

    if args.dry_run:
        output.success(
            "Dry run complete. No API calls or run artifacts were created."
        )
        return 0

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise CliError(
            "OPENAI_API_KEY is required for a real run.",
            before_api_calls=True,
        )

    if not args.yes and not _request_consent(input_fn, output):
        raise CliError(
            "External-data consent was not granted.",
            before_api_calls=True,
        )

    output_root = _prepare_output_root(args.output_dir, context)
    started_at = datetime.now(timezone.utc)
    execution_started = perf_counter()
    council_execution: CouncilExecution | None = None
    generalist_execution: GeneralistExecution | None = None

    try:
        if args.mode in {MODE_COUNCIL, MODE_BOTH}:
            council_execution = await run_council_with_telemetry(
                feature,
                context,
            )
        if args.mode in {MODE_GENERALIST, MODE_BOTH}:
            generalist_execution = await run_generalist(feature, context)
    except (CouncilOrchestrationError, EvaluationError) as error:
        raise CliError(str(error), before_api_calls=False) from error
    except Exception as error:
        raise CliError(
            f"Model execution failed: {error}",
            before_api_calls=False,
        ) from error

    total_runtime_ms = (perf_counter() - execution_started) * 1_000
    try:
        run_id, run_directory, report_path = _write_mode_artifacts(
            args.mode,
            feature,
            context,
            council_execution,
            generalist_execution,
            output_root,
            started_at,
        )
    except (EvaluationError, RunArtifactError, OSError, ValueError) as error:
        raise CliError(
            f"Artifact writing failed: {error}",
            before_api_calls=False,
        ) from error

    _print_completion(
        args.mode,
        run_id,
        run_directory,
        report_path,
        council_execution,
        generalist_execution,
        total_runtime_ms,
        output,
    )
    return 0


def build_preflight_estimate(
    feature: str,
    context: ContextBundle,
    mode: str,
    specialist_model: str | None,
    synthesis_model: str,
) -> PreflightEstimate:
    base_input_tokens = _characters_to_tokens(
        len(render_specialist_input(feature, context))
    )
    feature_tokens = _characters_to_tokens(len(feature))
    budgets: dict[str, _RoleBudget] = {}

    if mode in {MODE_COUNCIL, MODE_BOTH}:
        if specialist_model is None:
            raise ValueError("Council mode requires a specialist model.")
        for role in SPECIALIST_ROLES:
            prompt_tokens = _prompt_tokens(f"{role}.md")
            budgets[role] = _RoleBudget(
                model=specialist_model,
                input_tokens=base_input_tokens + prompt_tokens,
                output_tokens=OUTPUT_TOKEN_ALLOWANCES[role],
            )

        specialist_output_tokens = sum(
            OUTPUT_TOKEN_ALLOWANCES[role] for role in SPECIALIST_ROLES
        )
        budgets["producer"] = _RoleBudget(
            model=synthesis_model,
            input_tokens=(
                feature_tokens
                + specialist_output_tokens
                + _prompt_tokens("producer.md")
            ),
            output_tokens=OUTPUT_TOKEN_ALLOWANCES["producer"],
        )
        budgets["director"] = _RoleBudget(
            model=synthesis_model,
            input_tokens=(
                feature_tokens
                + specialist_output_tokens
                + OUTPUT_TOKEN_ALLOWANCES["producer"]
                + _prompt_tokens("director.md")
            ),
            output_tokens=OUTPUT_TOKEN_ALLOWANCES["director"],
        )

    if mode in {MODE_GENERALIST, MODE_BOTH}:
        budgets["generalist"] = _RoleBudget(
            model=synthesis_model,
            input_tokens=base_input_tokens + _prompt_tokens("generalist.md"),
            output_tokens=OUTPUT_TOKEN_ALLOWANCES["generalist"],
        )

    low_cost, low_unpriced = _estimate_budget_cost(budgets, Decimal("0.5"))
    high_cost, high_unpriced = _estimate_budget_cost(budgets, Decimal("1"))
    conservative_cost, conservative_unpriced = _estimate_budget_cost(
        budgets,
        Decimal(CONSERVATIVE_TOKEN_MULTIPLIER),
    )
    unpriced = tuple(
        sorted(low_unpriced | high_unpriced | conservative_unpriced)
    )
    return PreflightEstimate(
        expected_calls=len(budgets),
        rough_input_tokens=sum(
            budget.input_tokens for budget in budgets.values()
        ),
        output_token_allowance=sum(
            budget.output_tokens for budget in budgets.values()
        ),
        estimated_cost_low_usd=low_cost,
        estimated_cost_high_usd=high_cost,
        conservative_max_cost_usd=conservative_cost,
        unpriced_models=unpriced,
    )


def _estimate_budget_cost(
    budgets: dict[str, _RoleBudget],
    multiplier: Decimal,
) -> tuple[Decimal | None, set[str]]:
    roles = {
        role: RoleTelemetry(
            model=budget.model,
            duration_ms=0,
            usage=TokenUsage(
                requests=1,
                input_tokens=math.ceil(
                    Decimal(budget.input_tokens) * multiplier
                ),
                output_tokens=math.ceil(
                    Decimal(budget.output_tokens) * multiplier
                ),
                total_tokens=math.ceil(
                    Decimal(
                        budget.input_tokens + budget.output_tokens
                    )
                    * multiplier
                ),
            ),
        )
        for role, budget in budgets.items()
    }
    estimate = estimate_cost(roles, DEFAULT_PRICING_SNAPSHOT)
    return estimate.estimated_cost_usd, set(estimate.unpriced_models)


def _print_preflight(
    context: ContextBundle,
    mode: str,
    specialist_model: str | None,
    synthesis_model: str,
    estimate: PreflightEstimate,
    output: TerminalOutput,
) -> None:
    output.preflight(
        context,
        mode,
        specialist_model,
        synthesis_model,
        estimate,
    )


def _preflight_rows(
    context: ContextBundle,
    mode: str,
    specialist_model: str | None,
    synthesis_model: str,
    estimate: PreflightEstimate,
) -> tuple[tuple[str, str], ...]:
    rows = [
        ("Repository", context.repository_path),
        ("Branch", context.branch or "detached HEAD"),
        ("Commit SHA", context.commit_sha),
        ("Tracked working tree dirty", str(context.working_tree_dirty)),
        ("Selected context files", str(context.selected_file_count)),
        ("Context characters", str(context.total_text_characters)),
        ("Specialist model", specialist_model or "not used"),
        ("Synthesis model", synthesis_model),
        ("Requested mode", mode),
        ("Expected nominal model calls", str(estimate.expected_calls)),
        ("Rough estimated input tokens", str(estimate.rough_input_tokens)),
        ("Rough output-token allowance", str(estimate.output_token_allowance)),
    ]
    if estimate.estimated_cost_low_usd is None:
        models = ", ".join(estimate.unpriced_models) or "unknown"
        rows.extend(
            [
                ("Estimated cost range USD", f"unavailable ({models})"),
                (
                    "Conservative maximum cost USD",
                    f"unavailable ({models})",
                ),
            ]
        )
    else:
        rows.extend(
            [
                (
                    "Estimated cost range USD",
                    f"{_format_usd(estimate.estimated_cost_low_usd)}-"
                    f"{_format_usd(estimate.estimated_cost_high_usd)}",
                ),
                (
                    "Conservative maximum cost USD",
                    _format_usd(estimate.conservative_max_cost_usd),
                ),
            ]
        )
    rows.append(("Pricing snapshot", DEFAULT_PRICING_SNAPSHOT.identifier))
    return tuple(rows)


def _enforce_cost_cap(
    estimate: PreflightEstimate,
    maximum: Decimal | None,
) -> None:
    if maximum is None:
        return
    if estimate.conservative_max_cost_usd is None:
        models = ", ".join(estimate.unpriced_models) or "unknown"
        raise CliError(
            "The cost cap cannot be enforced because the pricing snapshot "
            f"does not price: {models}.",
            before_api_calls=True,
        )
    if estimate.conservative_max_cost_usd > maximum:
        raise CliError(
            "Conservative preflight cost "
            f"{_format_usd(estimate.conservative_max_cost_usd)} exceeds "
            f"--max-cost-usd {_format_usd(maximum)}.",
            before_api_calls=True,
        )


def _read_feature_source(
    feature: str | None,
    feature_file: str | None,
) -> str:
    if feature is not None and feature_file is not None:
        raise CliError(
            "Supply exactly one feature source: --feature or --feature-file.",
            before_api_calls=True,
        )
    if feature is not None:
        if not feature.strip():
            raise CliError(
                "Feature input is empty.",
                before_api_calls=True,
            )
        return feature
    if feature_file is None:
        raise CliError(
            "Supply exactly one feature source: --feature or --feature-file.",
            before_api_calls=True,
        )
    return _read_feature_file(feature_file)


def _read_feature_file(feature_file: str) -> str:
    path = Path(feature_file).expanduser().resolve()
    if not path.is_file():
        raise CliError(
            f"Feature file does not exist: {path}",
            before_api_calls=True,
        )
    try:
        feature = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CliError(
            f"Unable to read feature file as UTF-8 text: {path}: {error}",
            before_api_calls=True,
        ) from error
    if not feature.strip():
        raise CliError(
            f"Feature input is empty: {path}",
            before_api_calls=True,
        )
    return feature


def _read_model_configuration(mode: str) -> tuple[str | None, str]:
    specialist_model = os.environ.get("COUNCIL_SPECIALIST_MODEL", "").strip()
    synthesis_model = os.environ.get("COUNCIL_SYNTHESIS_MODEL", "").strip()
    missing: list[str] = []
    if mode in {MODE_COUNCIL, MODE_BOTH} and not specialist_model:
        missing.append("COUNCIL_SPECIALIST_MODEL")
    if not synthesis_model:
        missing.append("COUNCIL_SYNTHESIS_MODEL")
    if missing:
        raise CliError(
            "Missing model configuration: " + ", ".join(missing),
            before_api_calls=True,
        )
    active_specialist_model = (
        specialist_model if mode in {MODE_COUNCIL, MODE_BOTH} else None
    )
    return active_specialist_model, synthesis_model


def _request_consent(
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> bool:
    output.warning(
        "Repository-derived context will be sent to the OpenAI API and may "
        "incur paid usage."
    )
    try:
        response = input_fn("Proceed? [Y/N] ").strip().casefold()
    except EOFError:
        return False
    return response in {"y", "yes"}


def _prepare_output_root(
    output_dir: str,
    context: ContextBundle,
) -> Path:
    root = Path(output_dir).expanduser().resolve()
    repository = Path(context.repository_path).expanduser().resolve()
    if _is_within(root, repository):
        raise CliError(
            "Artifact output cannot be inside the analyzed repository.",
            before_api_calls=True,
        )
    if root.exists() and not root.is_dir():
        raise CliError(
            f"Artifact output is not a directory: {root}",
            before_api_calls=True,
        )
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CliError(
            f"Unable to create artifact output directory: {root}: {error}",
            before_api_calls=True,
        ) from error
    return root


def _write_mode_artifacts(
    mode: str,
    feature: str,
    context: ContextBundle,
    council_execution: CouncilExecution | None,
    generalist_execution: GeneralistExecution | None,
    output_root: Path,
    started_at: datetime,
) -> tuple[str, Path, Path]:
    from council.evaluation import create_comparison_record
    from council.reporting import (
        create_run_record,
        write_evaluation_artifacts,
        write_generalist_artifacts,
        write_run_artifacts,
    )

    if mode == MODE_GENERALIST:
        if generalist_execution is None:
            raise ValueError("Generalist execution is missing.")
        run_id = str(uuid4())
        run_directory = write_generalist_artifacts(
            run_id,
            feature,
            context,
            generalist_execution,
            output_root,
            started_at=started_at,
        )
        return run_id, run_directory, run_directory / "report.md"

    if council_execution is None:
        raise ValueError("Council execution is missing.")
    record = create_run_record(feature, council_execution)
    if mode == MODE_BOTH:
        if generalist_execution is None:
            raise ValueError("Generalist execution is missing.")
        comparison = create_comparison_record(record, generalist_execution)
    else:
        comparison = None

    run_directory = write_run_artifacts(record, output_root)
    if comparison is not None:
        write_evaluation_artifacts(run_directory, comparison)
    return record.run_id, run_directory, run_directory / "report.md"


def _print_completion(
    mode: str,
    run_id: str,
    run_directory: Path,
    report_path: Path,
    council: CouncilExecution | None,
    generalist: GeneralistExecution | None,
    total_runtime_ms: float,
    output: TerminalOutput,
) -> None:
    council_usage = (
        council.telemetry.total_usage if council is not None else TokenUsage()
    )
    generalist_usage = (
        generalist.telemetry.usage if generalist is not None else TokenUsage()
    )
    costs = [
        value
        for value in (
            _council_cost(council),
            generalist.estimated_cost_usd if generalist is not None else None,
        )
        if value is not None
    ]
    expected_cost_count = int(council is not None) + int(generalist is not None)
    total_cost = (
        sum(costs, Decimal("0"))
        if len(costs) == expected_cost_count
        else None
    )

    output.success("Run complete")
    output.line(f"Run ID: {run_id}")
    if council is not None:
        director = council.result.director
        output.line(f"Director decision: {director.decision.value}")
        output.line(f"Confidence: {director.confidence.value}")
    elif generalist is not None:
        output.line(f"Generalist decision: {generalist.result.decision.value}")
        output.line(f"Confidence: {generalist.result.confidence.value}")
    output.line(f"Council model calls: {council_usage.requests}")
    if generalist is not None:
        output.line(f"Generalist model calls: {generalist_usage.requests}")
    output.line(
        f"Actual input tokens: "
        f"{council_usage.input_tokens + generalist_usage.input_tokens}"
    )
    output.line(
        f"Actual output tokens: "
        f"{council_usage.output_tokens + generalist_usage.output_tokens}"
    )
    output.line(
        f"Actual total tokens: "
        f"{council_usage.total_tokens + generalist_usage.total_tokens}"
    )
    output.line(
        "Actual estimated cost USD: "
        + (_format_usd(total_cost) if total_cost is not None else "unavailable")
    )
    output.line(f"Total runtime: {total_runtime_ms:.3f} ms")
    output.line(f"Report: {report_path}")
    output.line(f"Run directory: {run_directory}")
    if mode == MODE_BOTH:
        output.line("Human comparison status: Pending")


def _council_cost(execution: CouncilExecution | None) -> Decimal | None:
    if execution is None:
        return None
    return estimate_cost(
        execution.telemetry.roles,
        DEFAULT_PRICING_SNAPSHOT,
    ).estimated_cost_usd


def _prompt_tokens(filename: str) -> int:
    return _characters_to_tokens(len(load_prompt(filename)))


def _characters_to_tokens(character_count: int) -> int:
    return max(1, math.ceil(character_count / CHARS_PER_TOKEN))


def _format_usd(value: Decimal | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value:.6f}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
