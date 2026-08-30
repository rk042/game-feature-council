import asyncio
import json
import math
import os
import subprocess
import sys
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
    FeatureRefinementRecord,
    FeatureRefinementRound,
    FeatureRefinerResult,
    GeneralistExecution,
    RoleTelemetry,
    TokenUsage,
)
from council.pricing import DEFAULT_PRICING_SNAPSHOT, estimate_cost
from council.presentation import ExecutionProgress
from council.progress import ProgressListener
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
    "feature_refiner": 1_200,
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
RECENT_REPOSITORY_LIMIT = 5


app = typer.Typer(
    name="council",
    help="Evaluate game-feature experiments against a Git repository.",
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)

config_app = typer.Typer(help="Manage local CLI convenience settings.")
app.add_typer(config_app, name="config")


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
    verbose: bool = False
    feature_refinement: FeatureRefinementRecord | None = None
    prior_api_calls: int = 0
    refinement_usage_unavailable: bool = False


@dataclass(frozen=True)
class ReviewArguments:
    run: str
    output_dir: str


@dataclass(frozen=True)
class RunCompletion:
    run_id: str
    run_directory: Path
    report_path: Path


@dataclass(frozen=True)
class _FeatureSelection:
    feature: str
    refinement: FeatureRefinementRecord | None = None
    prior_api_calls: int = 0
    usage_unavailable: bool = False


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

    @property
    def console(self) -> Console:
        return self._console


@app.callback(invoke_without_command=True)
def typer_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if not _is_interactive_terminal():
        typer.echo("No command supplied. Use `council run --help`.", err=True)
        raise typer.Exit(2)

    output = RichOutput()
    _exit_for_code(
        _run_with_error_boundary(
            "run",
            lambda: _interactive_wizard(input_fn=input, output=output),
            output,
        )
    )


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
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Show additional safe operational telemetry.",
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
        verbose=verbose,
    )
    _exit_for_code(
        _run_with_error_boundary(
            "run",
            lambda: asyncio.run(
                _run_command(
                    arguments,
                    input_fn=input,
                    output=output,
                    live_output=_is_live_terminal(output.console),
                )
            ),
            output,
        )
    )


@config_app.command("clear-recent")
def typer_clear_recent_command() -> None:
    output = RichOutput()
    try:
        cleared = clear_recent_repositories()
    except OSError as error:
        output.error(
            "Unable to clear recent repositories: "
            f"{_sanitize_configured_secret(str(error))}"
        )
        raise typer.Exit(2) from error
    if cleared:
        output.success("Recent repository history cleared.")
    else:
        output.line("Recent repository history is already empty.")


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
    if not argv:
        output = PlainOutput(print_fn)
        if not _is_interactive_terminal():
            output.error("No command supplied. Use `council run --help`.")
            return 2
        return _run_with_error_boundary(
            "run",
            lambda: _interactive_wizard(input_fn=input_fn, output=output),
            output,
        )
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
                    verbose=args.verbose,
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
        safe_message = _sanitize_configured_secret(str(error))
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


def _sanitize_configured_secret(text: str) -> str:
    return sanitize_user_facing_text(
        text,
        (os.environ.get("OPENAI_API_KEY", ""),),
    )


def _is_missing_agents_dependency(error: ModuleNotFoundError) -> bool:
    missing_name = error.name or ""
    return missing_name == "agents" or missing_name.startswith("agents.")


def build_context(repository_path: str, feature: str) -> ContextBundle:
    from council.context import build_context as build

    return build(repository_path, feature)


def validate_repository_path(repository_path: str) -> Path:
    from council.context import validate_repository_path as validate

    return validate(repository_path)


async def run_council_with_telemetry(
    feature: str,
    context: ContextBundle,
    *,
    progress_listener: ProgressListener | None = None,
) -> CouncilExecution:
    from council.orchestrator import run_council_with_telemetry as run

    return await run(
        feature,
        context,
        progress_listener=progress_listener,
    )


async def run_generalist(
    feature: str,
    context: ContextBundle,
    *,
    progress_listener: ProgressListener | None = None,
) -> GeneralistExecution:
    from council.evaluation import run_generalist as run

    return await run(
        feature,
        context,
        progress_listener=progress_listener,
    )


def render_specialist_input(feature: str, context: ContextBundle) -> str:
    from council.orchestrator import render_specialist_input as render

    return render(feature, context)


def load_prompt(filename: str) -> str:
    from council.agents import load_prompt as load

    return load(filename)


def _interactive_wizard(
    *,
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> int:
    output.heading("Game Feature Council")
    repository = _prompt_for_repository(input_fn, output)
    feature, feature_file = _prompt_for_feature(input_fn, output)
    mode = _prompt_for_mode(input_fn, output)
    _read_model_configuration(mode)
    maximum_cost = _prompt_for_maximum_cost(input_fn, output)
    original_feature = _read_feature_source(feature, feature_file)
    selection = asyncio.run(
        _run_feature_refinement_workflow(
            original_feature,
            maximum_cost,
            input_fn=input_fn,
            output=output,
        )
    )
    if selection is None:
        return 0

    completions: list[RunCompletion] = []
    code = asyncio.run(
        _run_command(
            RunArguments(
                repo=repository,
                feature=selection.feature,
                feature_file=None,
                mode=mode,
                max_cost_usd=maximum_cost,
                dry_run=False,
                yes=False,
                output_dir="runs",
                feature_refinement=selection.refinement,
                prior_api_calls=selection.prior_api_calls,
                refinement_usage_unavailable=selection.usage_unavailable,
            ),
            input_fn=input_fn,
            output=output,
            live_output=(
                isinstance(output, RichOutput)
                and _is_live_terminal(output.console)
            ),
            completion_callback=completions.append,
        )
    )
    if code != 0 or not completions:
        return code

    completion = completions[0]
    try:
        remember_repository(repository)
    except OSError as error:
        output.warning(
            "Run completed, but recent repository history could not be "
            f"updated: {_sanitize_configured_secret(str(error))}"
        )
    try:
        _prompt_for_post_run_action(
            completion,
            mode,
            input_fn=input_fn,
            output=output,
        )
    except KeyboardInterrupt:
        output.line("Post-run action cancelled. The completed run is unchanged.")
    except CliError as error:
        output.error(_sanitize_configured_secret(str(error)))
        output.line("The run completed, but the post-run action did not complete.")
        return 2
    return 0


def _prompt_for_repository(
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> str:
    from council.context import ContextBuilderError

    recent = load_recent_repositories()
    while True:
        output.heading("Repository")
        if recent:
            for index, repository in enumerate(recent, start=1):
                output.line(f"{index}. {repository}")
            output.line(f"{len(recent) + 1}. Enter another path")
            raw = _wizard_input(input_fn, "Selection or repository path: ")
            stripped = raw.strip()
            if stripped.isdigit():
                selection = int(stripped)
                if 1 <= selection <= len(recent):
                    candidate = recent[selection - 1]
                elif selection == len(recent) + 1:
                    candidate = _wizard_input(input_fn, "Repository path: ")
                else:
                    output.error("Choose a listed repository or enter a path.")
                    continue
            else:
                candidate = raw
        else:
            candidate = _wizard_input(input_fn, "Repository path: ")

        normalized = _normalize_pasted_path(candidate)
        if not normalized:
            output.error("Repository path is required.")
            continue
        try:
            repository = validate_repository_path(normalized)
        except ContextBuilderError as error:
            output.error(
                _sanitize_configured_secret(str(error))
            )
            output.line("Enter a valid Git working-tree path and try again.")
            continue
        return str(repository)


def _prompt_for_feature(
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> tuple[str | None, str | None]:
    if _prompt_yes_no(input_fn, "Use a feature file instead? [y/N] "):
        while True:
            raw_path = _wizard_input(input_fn, "Feature file path: ")
            path = _normalize_pasted_path(raw_path)
            try:
                feature = _read_feature_file(path)
            except CliError as error:
                output.error(_sanitize_configured_secret(str(error)))
                continue
            return feature, None

    output.heading("Feature request")
    output.line("Describe the feature. Press Enter on an empty line to finish.")
    while True:
        lines: list[str] = []
        while True:
            line = _wizard_input(input_fn, "> ")
            if not line:
                break
            lines.append(line)
        feature = "\n".join(lines)
        if feature.strip():
            return feature, None
        output.error("Feature input is empty. Please enter a feature request.")


def _prompt_for_mode(
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> str:
    output.heading("Analysis mode")
    output.line("1. Council + Generalist")
    output.line("2. Council only")
    output.line("3. Generalist only")
    modes = {
        "": MODE_BOTH,
        "1": MODE_BOTH,
        "2": MODE_COUNCIL,
        "3": MODE_GENERALIST,
    }
    while True:
        choice = _wizard_input(input_fn, "Selection [1]: ").strip()
        if choice in modes:
            return modes[choice]
        output.error("Choose 1, 2, or 3.")


def _prompt_for_maximum_cost(
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> Decimal | None:
    while True:
        raw = _wizard_input(
            input_fn,
            "Maximum API cost USD (leave blank for no cap): ",
        ).strip()
        if not raw:
            return None
        try:
            value = Decimal(raw)
        except InvalidOperation:
            output.error("Enter a non-negative decimal value or leave blank.")
            continue
        if not value.is_finite() or value < 0:
            output.error("Enter a non-negative decimal value or leave blank.")
            continue
        return value


async def _run_feature_refinement_workflow(
    original_feature: str,
    maximum_cost: Decimal | None,
    *,
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> _FeatureSelection | None:
    from council.refinement import (
        MAX_REFINEMENT_CALLS,
        FeatureRefinementError,
        configured_refiner_model,
        create_feature_refinement_record,
        refinement_round,
        run_feature_refiner,
    )

    output.heading("Feature Refiner")
    try:
        model = configured_refiner_model()
    except FeatureRefinementError as error:
        output.warning(
            f"Feature Refiner unavailable: {_sanitize_configured_secret(str(error))}"
        )
        output.line("Continuing with your original feature request.")
        return _FeatureSelection(feature=original_feature)

    estimate = build_refiner_preflight_estimate(original_feature, model)
    _print_refiner_preflight(
        model,
        estimate,
        Decimal("0"),
        output,
        correction_round=False,
    )
    output.warning(
        "Your feature description only will be sent to the configured AI "
        "model for interpretation. Repository content is not sent."
    )
    if not _prompt_yes_no(input_fn, "Run AI Feature Refiner? [y/N] "):
        output.line("Continuing with your original feature request.")
        return _FeatureSelection(feature=original_feature)
    try:
        _enforce_refinement_call_cost_cap(
            estimate,
            maximum_cost,
            Decimal("0"),
        )
    except CliError as error:
        output.error(str(error))
        output.line("Continuing with your original feature request.")
        return _FeatureSelection(feature=original_feature)
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        output.error("OPENAI_API_KEY is required to run Feature Refiner.")
        output.line("Continuing with your original feature request.")
        return _FeatureSelection(feature=original_feature)

    rounds: list[FeatureRefinementRound] = []
    previous_result: FeatureRefinerResult | None = None
    correction: str | None = None
    while len(rounds) < MAX_REFINEMENT_CALLS:
        output.line("Feature Refiner: Interpreting feature...")
        try:
            execution = await run_feature_refiner(
                original_feature,
                previous_result=previous_result,
                user_correction=correction,
                model=model,
            )
        except Exception as error:
            output.error(
                "Feature Refiner failed: "
                + _sanitize_configured_secret(str(error))
            )
            return _resolve_failed_refinement(
                original_feature,
                rounds,
                model=model,
                input_fn=input_fn,
                output=output,
            )

        rounds.append(
            refinement_round(execution, user_correction=correction)
        )
        output.success(
            "Feature Refiner: Interpretation ready "
            f"{execution.telemetry.duration_ms / 1_000:.1f}s; "
            f"{execution.telemetry.usage.total_tokens} tokens; "
            f"{_refinement_cost_text(execution.estimated_cost_usd, execution.unpriced_models)}"
        )
        completed_cost = _completed_refinement_cost(rounds)
        if (
            maximum_cost is not None
            and completed_cost is not None
            and completed_cost > maximum_cost
        ):
            output.error(
                "Completed Feature Refiner cost "
                f"{_format_usd(completed_cost)} exceeded the configured "
                f"session cap {_format_usd(maximum_cost)}."
            )
            _print_refinement_usage(rounds, output)
            output.line("No repository analysis or run artifacts were created.")
            return None

        previous_result = execution.result
        _print_refiner_interpretation(previous_result, output)
        if _prompt_refinement_confirmation(input_fn):
            record = create_feature_refinement_record(
                original_feature,
                rounds,
                approved=True,
            )
            output.success("Feature refinement completed and approved.")
            return _FeatureSelection(
                feature=record.approved_refined_feature or original_feature,
                refinement=record,
                prior_api_calls=len(rounds),
            )

        if len(rounds) >= MAX_REFINEMENT_CALLS:
            output.warning(
                f"The {MAX_REFINEMENT_CALLS}-call Feature Refiner limit was reached."
            )
            return _resolve_refinement_choice(
                original_feature,
                rounds,
                input_fn=input_fn,
                output=output,
            )

        correction = _prompt_for_refinement_correction(input_fn, output)
        next_estimate = build_refiner_preflight_estimate(
            original_feature,
            model,
            previous_result=previous_result,
            user_correction=correction,
        )
        _print_refiner_preflight(
            model,
            next_estimate,
            completed_cost,
            output,
            correction_round=True,
        )
        output.warning(
            "This correction requires another Feature Refiner AI call."
        )
        if not _prompt_refinement_yes_no(
            input_fn,
            "Continue? [y/N] ",
        ):
            return _resolve_refinement_choice(
                original_feature,
                rounds,
                input_fn=input_fn,
                output=output,
            )
        try:
            _enforce_refinement_call_cost_cap(
                next_estimate,
                maximum_cost,
                completed_cost,
            )
        except CliError as error:
            output.error(str(error))
            return _resolve_refinement_choice(
                original_feature,
                rounds,
                input_fn=input_fn,
                output=output,
            )
        estimate = next_estimate

    raise AssertionError("Feature Refiner loop exceeded its deterministic cap.")


def _print_refiner_interpretation(
    result: FeatureRefinerResult,
    output: TerminalOutput,
) -> None:
    output.heading("AI understood your feature as:")
    for item in result.concise_interpretation:
        output.line(f"- {item}")
    output.heading("Still not specified:")
    if result.unresolved_points:
        for item in result.unresolved_points:
            output.line(f"- {item}")
    else:
        output.line("- Nothing additional identified.")
    output.heading("Preserved constraints:")
    if result.preserved_constraints:
        for item in result.preserved_constraints:
            output.line(f"- {item}")
    else:
        output.line("- None explicitly stated.")
    output.heading("Proposed refined feature brief:")
    output.line(result.refined_brief)


def _prompt_refinement_confirmation(
    input_fn: Callable[[str], str],
) -> bool:
    response = _refinement_input(input_fn, "Is this what you mean? [Y/n] ")
    return response.strip().casefold() not in {"n", "no"}


def _prompt_for_refinement_correction(
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> str:
    output.heading("What should I correct?")
    while True:
        correction = _refinement_input(input_fn, "> ")
        if correction.strip():
            return correction
        output.error("Correction text is required.")


def _prompt_refinement_yes_no(
    input_fn: Callable[[str], str],
    prompt: str,
) -> bool:
    return _refinement_input(input_fn, prompt).strip().casefold() in {
        "y",
        "yes",
    }


def _refinement_input(input_fn: Callable[[str], str], prompt: str) -> str:
    try:
        return input_fn(prompt)
    except EOFError as error:
        raise CliError(
            "Interactive input ended after Feature Refiner execution.",
            before_api_calls=False,
        ) from error


def _resolve_refinement_choice(
    original_feature: str,
    rounds: list[FeatureRefinementRound],
    *,
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> _FeatureSelection | None:
    from council.refinement import create_feature_refinement_record

    output.line("1. Use the latest refined brief")
    output.line("2. Use the original request")
    output.line("3. Cancel")
    while True:
        choice = _refinement_input(input_fn, "Selection [3]: ").strip() or "3"
        if choice == "1":
            record = create_feature_refinement_record(
                original_feature,
                rounds,
                approved=True,
            )
            return _FeatureSelection(
                feature=record.approved_refined_feature or original_feature,
                refinement=record,
                prior_api_calls=len(rounds),
            )
        if choice == "2":
            record = create_feature_refinement_record(
                original_feature,
                rounds,
                approved=False,
            )
            output.line("Continuing with your original feature request.")
            return _FeatureSelection(
                feature=original_feature,
                refinement=record,
                prior_api_calls=len(rounds),
            )
        if choice == "3":
            _print_refinement_usage(rounds, output)
            output.line("No repository analysis or run artifacts were created.")
            return None
        output.error("Choose 1, 2, or 3.")


def _resolve_failed_refinement(
    original_feature: str,
    rounds: list[FeatureRefinementRound],
    *,
    model: str,
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> _FeatureSelection | None:
    from council.refinement import create_feature_refinement_record

    output.line("1. Continue using original feature")
    output.line("2. Cancel")
    while True:
        choice = _refinement_input(input_fn, "Selection [2]: ").strip() or "2"
        if choice == "1":
            _print_refinement_usage(rounds, output)
            output.warning(
                "The failed Refiner attempt has no trustworthy usage telemetry; "
                "provider billing, if any, is unavailable."
            )
            record = (
                create_feature_refinement_record(
                    original_feature,
                    rounds,
                    approved=False,
                    attempted_calls=len(rounds) + 1,
                    usage_complete=False,
                    usage_unavailable_reason=(
                        "One Feature Refiner attempt did not return usage "
                        "telemetry."
                    ),
                    model=rounds[-1].telemetry.model if rounds else model,
                )
            )
            return _FeatureSelection(
                feature=original_feature,
                refinement=record,
                prior_api_calls=record.attempted_calls,
                usage_unavailable=True,
            )
        if choice == "2":
            _print_refinement_usage(rounds, output)
            output.line("No repository analysis or run artifacts were created.")
            return None
        output.error("Choose 1 or 2.")


def _print_refinement_usage(
    rounds: list[FeatureRefinementRound],
    output: TerminalOutput,
) -> None:
    calls = len(rounds)
    tokens = sum(item.telemetry.usage.total_tokens for item in rounds)
    cost = _completed_refinement_cost(rounds)
    output.line(f"Feature Refiner completed calls: {calls}")
    output.line(f"Feature Refiner tokens: {tokens}")
    output.line(
        "Feature Refiner estimated cost USD: "
        + (_format_usd(cost) if cost is not None else "unpriced")
    )


def _prompt_for_post_run_action(
    completion: RunCompletion,
    mode: str,
    *,
    input_fn: Callable[[str], str],
    output: TerminalOutput,
) -> None:
    while True:
        output.heading("What next?")
        output.line("1. Review decision")
        output.line("2. Open report")
        output.line("3. Exit")
        choice = _wizard_input(input_fn, "Selection [3]: ").strip() or "3"
        if choice == "1":
            if mode == MODE_GENERALIST:
                output.line("Human review is not available for Generalist-only runs.")
                continue
            _review_command(
                ReviewArguments(
                    run=completion.run_id,
                    output_dir=str(completion.run_directory.parent),
                ),
                input_fn=input_fn,
                print_fn=output.line,
            )
            return
        if choice == "2":
            try:
                open_report(completion.report_path)
            except (OSError, RuntimeError) as error:
                output.warning(
                    "Unable to open the report: "
                    f"{_sanitize_configured_secret(str(error))}\n"
                    f"Report: {completion.report_path}"
                )
            return
        if choice == "3":
            return
        output.error("Choose 1, 2, or 3.")


def open_report(report_path: Path) -> None:
    path = report_path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise OSError(f"Report does not exist: {path}")
    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise RuntimeError("Windows report opener is unavailable.")
        startfile(str(path))
        return
    command = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [command, str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _prompt_yes_no(
    input_fn: Callable[[str], str],
    prompt: str,
) -> bool:
    return _wizard_input(input_fn, prompt).strip().casefold() in {"y", "yes"}


def _wizard_input(input_fn: Callable[[str], str], prompt: str) -> str:
    try:
        return input_fn(prompt)
    except EOFError as error:
        raise CliError(
            "Interactive input ended before the wizard was complete.",
            before_api_calls=True,
        ) from error


def _normalize_pasted_path(value: str) -> str:
    normalized = value.strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"'", '"'}
    ):
        return normalized[1:-1]
    return normalized


def load_recent_repositories() -> list[str]:
    path = _recent_repository_store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("repositories"), list):
        return []

    repositories: list[str] = []
    seen: set[str] = set()
    for value in raw["repositories"]:
        if not isinstance(value, str) or not value.strip():
            continue
        path_value = Path(value).expanduser()
        if not path_value.is_dir():
            continue
        try:
            canonical = str(validate_repository_path(value))
        except Exception:
            continue
        identity = os.path.normcase(canonical)
        if identity in seen:
            continue
        seen.add(identity)
        repositories.append(canonical)
        if len(repositories) == RECENT_REPOSITORY_LIMIT:
            break
    return repositories


def remember_repository(repository: str) -> None:
    repository_path = Path(repository).expanduser().resolve()
    canonical = str(repository_path)
    existing = load_recent_repositories()
    identity = os.path.normcase(canonical)
    repositories = [
        canonical,
        *[
            value
            for value in existing
            if os.path.normcase(value) != identity
        ],
    ][:RECENT_REPOSITORY_LIMIT]
    path = _recent_repository_store_path().expanduser().resolve()
    if _is_within(path, repository_path):
        raise OSError(
            "Recent repository history location resolves inside the "
            "analyzed repository; history was not written."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"repositories": repositories}, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def clear_recent_repositories() -> bool:
    path = _recent_repository_store_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _recent_repository_store_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / "game-feature-council" / "recent-repositories.json"


def _is_interactive_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _is_live_terminal(console: Console) -> bool:
    return bool(console.is_terminal and sys.stdout.isatty())


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
                    *_review_report_paths(artifacts.run_directory),
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
                    *_review_report_paths(artifacts.run_directory),
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


def _review_report_paths(run_directory: Path) -> list[Path]:
    paths = [run_directory / "report.md"]
    html_path = run_directory / "report.html"
    if html_path.is_file() and not html_path.is_symlink():
        paths.append(html_path)
    return paths


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
    live_output: bool = False,
    completion_callback: Callable[[RunCompletion], None] | None = None,
) -> int:
    from council.context import ContextBuilderError
    from council.evaluation import EvaluationError
    from council.orchestrator import CouncilOrchestrationError

    before_any_api_calls = args.prior_api_calls == 0
    feature = _read_feature_source(args.feature, args.feature_file)
    output.line("Context Builder: Building repository context")
    context_started = perf_counter()
    try:
        context = build_context(args.repo, feature)
    except ContextBuilderError as error:
        raise CliError(
            str(error),
            before_api_calls=before_any_api_calls,
        ) from error

    try:
        specialist_model, synthesis_model = _read_model_configuration(
            args.mode
        )
    except CliError as error:
        raise CliError(
            str(error),
            before_api_calls=before_any_api_calls,
        ) from error
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
    _print_session_preflight(
        preflight,
        args.feature_refinement,
        args.refinement_usage_unavailable,
        args.max_cost_usd,
        output,
    )
    try:
        _enforce_cost_cap(
            preflight,
            args.max_cost_usd,
            completed_cost=(
                args.feature_refinement.estimated_cost_usd
                if args.feature_refinement is not None
                else Decimal("0")
            ),
            prior_usage_unavailable=args.refinement_usage_unavailable,
        )
    except CliError as error:
        raise CliError(
            str(error),
            before_api_calls=before_any_api_calls,
        ) from error

    if args.dry_run:
        output.success(
            "Dry run complete. No API calls or run artifacts were created."
        )
        return 0

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise CliError(
            "OPENAI_API_KEY is required for a real run.",
            before_api_calls=before_any_api_calls,
        )

    if not args.yes and not _request_consent(input_fn, output):
        raise CliError(
            "External-data consent was not granted.",
            before_api_calls=before_any_api_calls,
        )

    try:
        output_root = _prepare_output_root(args.output_dir, context)
    except CliError as error:
        raise CliError(
            str(error),
            before_api_calls=before_any_api_calls,
        ) from error
    started_at = datetime.now(timezone.utc)
    execution_started = perf_counter()
    council_execution: CouncilExecution | None = None
    generalist_execution: GeneralistExecution | None = None
    presenter = ExecutionProgress(
        args.mode,
        print_fn=output.line,
        console=(output.console if isinstance(output, RichOutput) else None),
        live=live_output,
        verbose=args.verbose,
    )
    presenter.complete_context(
        (perf_counter() - context_started) * 1_000,
        context.selected_file_count,
        context.total_text_characters,
    )
    presenter.start()
    refresh_task = (
        asyncio.create_task(_refresh_progress(presenter))
        if live_output
        else None
    )

    try:
        if args.mode in {MODE_COUNCIL, MODE_BOTH}:
            council_execution = await run_council_with_telemetry(
                feature,
                context,
                progress_listener=presenter,
            )
        if args.mode in {MODE_GENERALIST, MODE_BOTH}:
            generalist_execution = await run_generalist(
                feature,
                context,
                progress_listener=presenter,
            )
    except asyncio.CancelledError:
        presenter.fail_remaining()
        await _stop_progress(presenter, refresh_task)
        raise
    except (CouncilOrchestrationError, EvaluationError) as error:
        presenter.fail_remaining()
        await _stop_progress(presenter, refresh_task)
        raise CliError(str(error), before_api_calls=False) from error
    except Exception as error:
        presenter.fail_remaining()
        await _stop_progress(presenter, refresh_task)
        raise CliError(
            f"Model execution failed: {error}",
            before_api_calls=False,
        ) from error

    total_runtime_ms = (perf_counter() - execution_started) * 1_000
    presenter.start_artifacts()
    artifact_started = perf_counter()
    try:
        run_id, run_directory, report_path = _write_mode_artifacts(
            args.mode,
            feature,
            context,
            council_execution,
            generalist_execution,
            output_root,
            started_at,
            feature_refinement=args.feature_refinement,
        )
    except (EvaluationError, RunArtifactError, OSError, ValueError) as error:
        presenter.fail_remaining()
        await _stop_progress(presenter, refresh_task)
        raise CliError(
            f"Artifact writing failed: {error}",
            before_api_calls=False,
        ) from error
    except Exception as error:
        presenter.fail_remaining()
        await _stop_progress(presenter, refresh_task)
        raise CliError(
            f"Artifact writing failed: {error}",
            before_api_calls=False,
        ) from error

    presenter.complete_artifacts((perf_counter() - artifact_started) * 1_000)
    await _stop_progress(presenter, refresh_task)

    _print_completion(
        args.mode,
        run_id,
        run_directory,
        report_path,
        council_execution,
        generalist_execution,
        total_runtime_ms,
        output,
        feature_refinement=args.feature_refinement,
        refinement_usage_unavailable=args.refinement_usage_unavailable,
    )
    if completion_callback is not None:
        completion_callback(
            RunCompletion(
                run_id=run_id,
                run_directory=run_directory,
                report_path=report_path,
            )
        )
    return 0


async def _refresh_progress(presenter: ExecutionProgress) -> None:
    try:
        while True:
            await asyncio.sleep(0.25)
            presenter.refresh()
    except asyncio.CancelledError:
        return


async def _stop_progress(
    presenter: ExecutionProgress,
    refresh_task: asyncio.Task[None] | None,
) -> None:
    if refresh_task is not None:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
    presenter.stop()


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


def build_refiner_preflight_estimate(
    original_feature: str,
    model: str,
    *,
    previous_result: FeatureRefinerResult | None = None,
    user_correction: str | None = None,
) -> PreflightEstimate:
    from council.refinement import render_feature_refiner_input

    input_text = render_feature_refiner_input(
        original_feature,
        previous_result=previous_result,
        user_correction=user_correction,
    )
    budget = _RoleBudget(
        model=model,
        input_tokens=(
            _characters_to_tokens(len(input_text))
            + _prompt_tokens("feature_refiner.md")
        ),
        output_tokens=OUTPUT_TOKEN_ALLOWANCES["feature_refiner"],
    )
    budgets = {"feature_refiner": budget}
    low_cost, low_unpriced = _estimate_budget_cost(
        budgets,
        Decimal("0.5"),
    )
    high_cost, high_unpriced = _estimate_budget_cost(
        budgets,
        Decimal("1"),
    )
    conservative_cost, conservative_unpriced = _estimate_budget_cost(
        budgets,
        Decimal(CONSERVATIVE_TOKEN_MULTIPLIER),
    )
    return PreflightEstimate(
        expected_calls=1,
        rough_input_tokens=budget.input_tokens,
        output_token_allowance=budget.output_tokens,
        estimated_cost_low_usd=low_cost,
        estimated_cost_high_usd=high_cost,
        conservative_max_cost_usd=conservative_cost,
        unpriced_models=tuple(
            sorted(low_unpriced | high_unpriced | conservative_unpriced)
        ),
    )


def _print_refiner_preflight(
    model: str,
    estimate: PreflightEstimate,
    completed_cost: Decimal | None,
    output: TerminalOutput,
    *,
    correction_round: bool,
) -> None:
    output.heading("Feature Refiner preflight")
    output.line(f"Model: {model}")
    output.line("Expected nominal calls: 1")
    output.line(f"Rough estimated input tokens: {estimate.rough_input_tokens}")
    output.line(f"Output-token allowance: {estimate.output_token_allowance}")
    if estimate.estimated_cost_low_usd is None:
        models = ", ".join(estimate.unpriced_models) or "unknown"
        output.line(f"Estimated cost: unavailable ({models})")
    else:
        output.line(
            "Estimated cost range USD: "
            f"{_format_usd(estimate.estimated_cost_low_usd)}-"
            f"{_format_usd(estimate.estimated_cost_high_usd)}"
        )
        output.line(
            "Conservative next-call cost USD: "
            f"{_format_usd(estimate.conservative_max_cost_usd)}"
        )
    if completed_cost is not None:
        output.line(
            "Completed refinement cost USD: "
            f"{_format_usd(completed_cost)}"
        )
    if correction_round:
        output.line(
            "Data sent: original feature description, previous typed "
            "interpretation, and your correction only"
        )
    else:
        output.line("Data sent: feature description only")
    output.line(f"Pricing snapshot: {DEFAULT_PRICING_SNAPSHOT.identifier}")


def _enforce_refinement_call_cost_cap(
    estimate: PreflightEstimate,
    maximum: Decimal | None,
    completed_cost: Decimal | None,
) -> None:
    if maximum is None:
        return
    if completed_cost is None:
        raise CliError(
            "The cumulative cost cap cannot be enforced because completed "
            "Feature Refiner usage is unpriced.",
            before_api_calls=True,
        )
    if estimate.conservative_max_cost_usd is None:
        models = ", ".join(estimate.unpriced_models) or "unknown"
        raise CliError(
            "The Feature Refiner cost cap cannot be enforced because the "
            f"pricing snapshot does not price: {models}.",
            before_api_calls=True,
        )
    session_maximum = completed_cost + estimate.conservative_max_cost_usd
    if session_maximum > maximum:
        raise CliError(
            "Feature Refiner cumulative conservative cost "
            f"{_format_usd(session_maximum)} exceeds the configured session "
            f"cap {_format_usd(maximum)}.",
            before_api_calls=True,
        )


def _completed_refinement_cost(
    rounds: Sequence[FeatureRefinementRound],
) -> Decimal | None:
    if not rounds:
        return None
    costs = [item.estimated_cost_usd for item in rounds]
    if any(cost is None for cost in costs):
        return None
    return sum(
        (cost for cost in costs if cost is not None),
        Decimal("0"),
    )


def _refinement_cost_text(
    cost: Decimal | None,
    unpriced_models: Sequence[str],
) -> str:
    if cost is not None:
        return f"estimated cost {_format_usd(cost)}"
    models = ", ".join(unpriced_models) or "unknown"
    return f"cost unpriced ({models})"


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
    *,
    completed_cost: Decimal | None = Decimal("0"),
    prior_usage_unavailable: bool = False,
) -> None:
    if maximum is None:
        return
    if prior_usage_unavailable or completed_cost is None:
        raise CliError(
            "The cumulative session cost cap cannot be enforced because "
            "completed Feature Refiner usage is unavailable or unpriced.",
            before_api_calls=True,
        )
    if estimate.conservative_max_cost_usd is None:
        models = ", ".join(estimate.unpriced_models) or "unknown"
        raise CliError(
            "The cost cap cannot be enforced because the pricing snapshot "
            f"does not price: {models}.",
            before_api_calls=True,
        )
    session_maximum = completed_cost + estimate.conservative_max_cost_usd
    if session_maximum > maximum:
        if completed_cost == 0:
            message = (
                "Conservative preflight cost "
                f"{_format_usd(estimate.conservative_max_cost_usd)} exceeds "
                f"--max-cost-usd {_format_usd(maximum)}."
            )
        else:
            message = (
                "Cumulative conservative session cost "
                f"{_format_usd(session_maximum)} exceeds --max-cost-usd "
                f"{_format_usd(maximum)}."
            )
        raise CliError(
            message,
            before_api_calls=True,
        )


def _print_session_preflight(
    downstream: PreflightEstimate,
    refinement: FeatureRefinementRecord | None,
    refinement_usage_unavailable: bool,
    maximum: Decimal | None,
    output: TerminalOutput,
) -> None:
    if refinement is None and not refinement_usage_unavailable:
        return
    output.heading("Session call and cost summary")
    if refinement is not None:
        output.line(
            f"Feature Refiner completed calls: {refinement.refinement_rounds}"
        )
        output.line(
            "Feature Refiner actual estimated cost USD: "
            + (
                _format_usd(refinement.estimated_cost_usd)
                if refinement.estimated_cost_usd is not None
                else "unpriced"
            )
        )
    else:
        output.line("Feature Refiner attempted usage: unavailable")
    output.line(f"Downstream expected calls: {downstream.expected_calls}")
    if refinement is not None:
        output.line(
            "Total session calls after completion: "
            f"{refinement.refinement_rounds + downstream.expected_calls}"
        )
    else:
        output.line("Total session calls after completion: unavailable")
    if maximum is not None and refinement is not None:
        completed = refinement.estimated_cost_usd
        if completed is not None:
            output.line(
                "Remaining configured budget USD: "
                f"{_format_usd(maximum - completed)}"
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
    *,
    feature_refinement: FeatureRefinementRecord | None = None,
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
            feature_refinement=feature_refinement,
        )
        return run_id, run_directory, _preferred_report_path(run_directory)

    if council_execution is None:
        raise ValueError("Council execution is missing.")
    record = create_run_record(feature, council_execution)
    if mode == MODE_BOTH:
        if generalist_execution is None:
            raise ValueError("Generalist execution is missing.")
        comparison = create_comparison_record(record, generalist_execution)
    else:
        comparison = None

    run_directory = write_run_artifacts(
        record,
        output_root,
        feature_refinement=feature_refinement,
    )
    if comparison is not None:
        write_evaluation_artifacts(run_directory, comparison)
    return record.run_id, run_directory, _preferred_report_path(run_directory)


def _preferred_report_path(run_directory: Path) -> Path:
    html_path = run_directory / "report.html"
    if html_path.is_file() and not html_path.is_symlink():
        return html_path
    return run_directory / "report.md"


def _print_completion(
    mode: str,
    run_id: str,
    run_directory: Path,
    report_path: Path,
    council: CouncilExecution | None,
    generalist: GeneralistExecution | None,
    total_runtime_ms: float,
    output: TerminalOutput,
    *,
    feature_refinement: FeatureRefinementRecord | None = None,
    refinement_usage_unavailable: bool = False,
) -> None:
    usage_complete = (
        not refinement_usage_unavailable
        and (
            feature_refinement is None
            or feature_refinement.usage_complete
        )
    )
    council_usage = (
        council.telemetry.total_usage if council is not None else TokenUsage()
    )
    generalist_usage = (
        generalist.telemetry.usage if generalist is not None else TokenUsage()
    )
    costs: list[Decimal] = [
        value
        for value in (
            _council_cost(council),
            generalist.estimated_cost_usd if generalist is not None else None,
        )
        if value is not None
    ]
    expected_cost_count = int(council is not None) + int(generalist is not None)
    if feature_refinement is not None:
        if feature_refinement.estimated_cost_usd is not None:
            costs.append(feature_refinement.estimated_cost_usd)
        expected_cost_count += 1
    total_cost = (
        sum(costs, Decimal("0"))
        if len(costs) == expected_cost_count
        and usage_complete
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
    refinement_usage = (
        feature_refinement.telemetry.usage
        if feature_refinement is not None
        else TokenUsage()
    )
    if feature_refinement is not None:
        output.line(
            "Feature Refiner attempted calls: "
            f"{feature_refinement.attempted_calls}"
        )
        output.line(
            "Feature Refiner completed calls: "
            f"{feature_refinement.refinement_rounds}"
        )
        output.line(
            (
                "Known Feature Refiner cost USD: "
                if not feature_refinement.usage_complete
                else "Feature Refiner estimated cost USD: "
            )
            + (
                "unavailable"
                if (
                    not feature_refinement.usage_complete
                    and not feature_refinement.rounds
                )
                else (
                    _format_usd(feature_refinement.estimated_cost_usd)
                    if feature_refinement.estimated_cost_usd is not None
                    else "unpriced"
                )
            )
        )
    if not usage_complete:
        output.warning(
            "Feature Refiner failed-attempt usage is unavailable; session "
            "call, token, and cost totals may be incomplete."
        )
    else:
        output.line(
            "Total session model calls: "
            f"{council_usage.requests + generalist_usage.requests + (feature_refinement.refinement_rounds if feature_refinement is not None else 0)}"
        )
    output.line(
        f"Actual input tokens: "
        f"{council_usage.input_tokens + generalist_usage.input_tokens + refinement_usage.input_tokens}"
    )
    output.line(
        f"Actual output tokens: "
        f"{council_usage.output_tokens + generalist_usage.output_tokens + refinement_usage.output_tokens}"
    )
    output.line(
        f"Actual total tokens: "
        f"{council_usage.total_tokens + generalist_usage.total_tokens + refinement_usage.total_tokens}"
    )
    if usage_complete:
        output.line(
            "Actual estimated cost USD: "
            + (
                _format_usd(total_cost)
                if total_cost is not None
                else "unavailable"
            )
        )
    else:
        known_cost = sum(costs, Decimal("0")) if costs else Decimal("0")
        output.line(f"Known estimated cost USD: {_format_usd(known_cost)}")
        output.line("Overall session cost: incomplete")
        output.line(
            "Reason: "
            + (
                feature_refinement.usage_unavailable_reason
                if feature_refinement is not None
                and feature_refinement.usage_unavailable_reason
                else "A Feature Refiner attempt did not return usage telemetry."
            )
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
