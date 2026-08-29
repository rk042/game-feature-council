import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from council.cli import (
    app,
    build_preflight_estimate,
    main,
    parse_args,
    sanitize_user_facing_text,
)
from council.evaluation import build_context_identity
from council.models import (
    ContextBuilderConfig,
    ContextBundle,
    CouncilExecution,
    CouncilResult,
    CouncilTelemetry,
    GeneralistExecution,
    RepositoryEvidence,
    RoleTelemetry,
    TokenUsage,
)
from council.orchestrator import CouncilOrchestrationError
from council.reporting import (
    EXPECTED_ARTIFACT_FILES,
    EXPECTED_EVALUATION_ARTIFACT_FILES,
    EXPECTED_GENERALIST_ARTIFACT_FILES,
)
from typer.testing import CliRunner
from test_director_agent import PRODUCER
from test_orchestrator import DIRECTOR
from test_producer_agent import (
    ANALYTICS,
    FEATURE,
    GAME_DESIGN,
    SCOPE_RISK,
    TECHNICAL,
)


MODEL_ENVIRONMENT = {
    "COUNCIL_SPECIALIST_MODEL": "gpt-5.4-nano",
    "COUNCIL_SYNTHESIS_MODEL": "gpt-5.4-nano",
    "OPENAI_API_KEY": "synthetic-secret-key",
}
ROLE_NAMES = (
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
    "producer",
    "director",
)
STARTED_AT = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


class CliTests(unittest.TestCase):
    def test_typer_help_exposes_run_and_review_commands(self) -> None:
        runner = CliRunner()

        cases = (
            (["--help"], ("run", "review")),
            (["run", "--help"], ("--repo", "--feature", "--feature-file")),
            (["review", "--help"], ("--run", "--output-dir")),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = runner.invoke(app, arguments)

                self.assertEqual(result.exit_code, 0, result.output)
                for text in expected:
                    self.assertIn(text, result.output)

    def test_user_facing_sanitizer_is_complete_safe_and_idempotent(self) -> None:
        secret = MODEL_ENVIRONMENT["OPENAI_API_KEY"]
        original = (
            f"Authorization: Bearer {secret}; url=?key={secret}; "
            "provider unavailable"
        )

        sanitized = sanitize_user_facing_text(original, (secret,))

        self.assertNotIn(secret, sanitized)
        self.assertEqual(sanitized.count("[REDACTED]"), 2)
        self.assertIn("Authorization: Bearer [REDACTED]", sanitized)
        self.assertIn("provider unavailable", sanitized)
        self.assertEqual(
            sanitize_user_facing_text(sanitized, (secret,)),
            sanitized,
        )
        self.assertEqual(
            sanitize_user_facing_text("normal readable error", ("",)),
            "normal readable error",
        )
        self.assertEqual(
            sanitize_user_facing_text("normal readable error", ("   ",)),
            "normal readable error",
        )
        self.assertEqual(
            sanitize_user_facing_text("normal readable error", ()),
            "normal readable error",
        )

    def test_argument_parsing_defaults_and_modes(self) -> None:
        default = parse_args(
            ["run", "--repo", "repo", "--feature-file", "feature.txt"]
        )
        self.assertEqual(default.mode, "both")
        self.assertFalse(default.dry_run)
        self.assertFalse(default.yes)
        self.assertIsNone(default.max_cost_usd)

        for mode, expected_calls in (
            ("council", 6),
            ("generalist", 1),
            ("both", 7),
        ):
            with self.subTest(mode=mode):
                args = parse_args(
                    [
                        "run",
                        "--repo",
                        "repo",
                        "--feature-file",
                        "feature.txt",
                        "--mode",
                        mode,
                        "--max-cost-usd",
                        "0.10",
                    ]
                )
                self.assertEqual(args.mode, mode)
                self.assertEqual(args.max_cost_usd, Decimal("0.10"))
                estimate = build_preflight_estimate(
                    FEATURE,
                    self._context("/synthetic/repository"),
                    mode,
                    None if mode == "generalist" else "gpt-5.4-nano",
                    "gpt-5.4-nano",
                )
                self.assertEqual(estimate.expected_calls, expected_calls)

    def test_missing_and_empty_feature_input_fail_before_context(self) -> None:
        output: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            missing = root / "missing.txt"
            empty = root / "empty.txt"
            empty.write_text(" \n\t", encoding="utf-8")
            build = patch("council.cli.build_context")

            with build as build_context_mock:
                missing_code = main(
                    [
                        "run",
                        "--repo",
                        str(root),
                        "--feature-file",
                        str(missing),
                        "--dry-run",
                    ],
                    print_fn=output.append,
                )
                empty_code = main(
                    [
                        "run",
                        "--repo",
                        str(root),
                        "--feature-file",
                        str(empty),
                        "--dry-run",
                    ],
                    print_fn=output.append,
                )

            self.assertEqual(missing_code, 2)
            self.assertEqual(empty_code, 2)
            build_context_mock.assert_not_called()
            rendered = "\n".join(output)
            self.assertIn("Feature file does not exist", rendered)
            self.assertIn("Feature input is empty", rendered)

    def test_direct_feature_reaches_context_unchanged_in_typer_dry_run(
        self,
    ) -> None:
        direct_feature = "  Add a reversible shared objective.  "
        runner = CliRunner()
        context = self._context("/synthetic/repository")
        council = AsyncMock()
        generalist = AsyncMock()

        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch("council.cli.build_context", return_value=context) as build,
            patch("council.cli.run_council_with_telemetry", council),
            patch("council.cli.run_generalist", generalist),
        ):
            result = runner.invoke(
                app,
                [
                    "run",
                    "--repo",
                    context.repository_path,
                    "--feature",
                    direct_feature,
                    "--dry-run",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        build.assert_called_once_with(context.repository_path, direct_feature)
        council.assert_not_awaited()
        generalist.assert_not_awaited()
        self.assertIn("Preflight estimate", result.output)
        self.assertIn("No API calls or run artifacts were created", result.output)

    def test_feature_sources_are_exclusive_and_required_before_context(
        self,
    ) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feature_file = self._feature_file(root)
            with patch("council.cli.build_context") as build_context_mock:
                both = runner.invoke(
                    app,
                    [
                        "run",
                        "--repo",
                        str(root),
                        "--feature",
                        "Direct feature",
                        "--feature-file",
                        str(feature_file),
                        "--dry-run",
                    ],
                )
                neither = runner.invoke(
                    app,
                    ["run", "--repo", str(root), "--dry-run"],
                )

            self.assertEqual(both.exit_code, 2, both.output)
            self.assertEqual(neither.exit_code, 2, neither.output)
            build_context_mock.assert_not_called()
            self.assertIn("Supply exactly one feature source", both.output)
            self.assertIn("Supply exactly one feature source", neither.output)
            self.assertNotIn("Traceback", both.output)
            self.assertNotIn("Traceback", neither.output)

    def test_whitespace_only_direct_feature_fails_before_context(self) -> None:
        output: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch("council.cli.build_context") as build_context_mock:
                code = main(
                    [
                        "run",
                        "--repo",
                        str(root),
                        "--feature",
                        " \t\n ",
                        "--dry-run",
                    ],
                    print_fn=output.append,
                )

        self.assertEqual(code, 2)
        build_context_mock.assert_not_called()
        self.assertIn("Feature input is empty", "\n".join(output))

    def test_invalid_cost_cap_is_a_clean_typer_usage_error(self) -> None:
        runner = CliRunner()

        with patch("council.cli.build_context") as build_context_mock:
            result = runner.invoke(
                app,
                [
                    "run",
                    "--repo",
                    "/synthetic/repository",
                    "--feature",
                    "Direct feature",
                    "--max-cost-usd",
                    "-1",
                ],
            )

        self.assertEqual(result.exit_code, 2, result.output)
        build_context_mock.assert_not_called()
        self.assertIn("non-negative decimal", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_invalid_repository_returns_nonzero_before_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feature_file = root / "feature.txt"
            feature_file.write_text(FEATURE, encoding="utf-8")
            output: list[str] = []

            with patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False):
                code = main(
                    [
                        "run",
                        "--repo",
                        str(root),
                        "--feature-file",
                        str(feature_file),
                        "--dry-run",
                    ],
                    print_fn=output.append,
                )

            self.assertEqual(code, 2)
            self.assertIn("Git working tree", "\n".join(output))
            self.assertIn("before any API calls", "\n".join(output))

    def test_dry_run_prints_preflight_and_makes_zero_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feature_file = self._feature_file(root)
            output_root = root / "runs"
            context = self._context(str(root / "target"))
            council = AsyncMock()
            generalist = AsyncMock()
            output: list[str] = []

            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", return_value=context),
                patch(
                    "council.cli.run_council_with_telemetry",
                    council,
                ),
                patch("council.cli.run_generalist", generalist),
            ):
                code = main(
                    [
                        "run",
                        "--repo",
                        context.repository_path,
                        "--feature-file",
                        str(feature_file),
                        "--dry-run",
                        "--output-dir",
                        str(output_root),
                    ],
                    print_fn=output.append,
                )

            self.assertEqual(code, 0)
            council.assert_not_awaited()
            generalist.assert_not_awaited()
            self.assertFalse(output_root.exists())
            rendered = "\n".join(output)
            for expected in (
                f"Repository: {context.repository_path}",
                "Branch: example-branch",
                f"Commit SHA: {context.commit_sha}",
                "Tracked working tree dirty: True",
                "Selected context files: 1",
                f"Context characters: {context.total_text_characters}",
                "Specialist model: gpt-5.4-nano",
                "Synthesis model: gpt-5.4-nano",
                "Requested mode: both",
                "Expected nominal model calls: 7",
                "Estimated cost range USD:",
                "Conservative maximum cost USD:",
                "No API calls or run artifacts were created",
            ):
                self.assertIn(expected, rendered)
            self.assertNotIn(MODEL_ENVIRONMENT["OPENAI_API_KEY"], rendered)

    def test_cost_cap_rejects_before_model_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feature_file = self._feature_file(root)
            context = self._context(str(root / "target"))
            council = AsyncMock()
            output: list[str] = []

            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", return_value=context),
                patch(
                    "council.cli.run_council_with_telemetry",
                    council,
                ),
            ):
                code = main(
                    [
                        "run",
                        "--repo",
                        context.repository_path,
                        "--feature-file",
                        str(feature_file),
                        "--mode",
                        "council",
                        "--max-cost-usd",
                        "0",
                        "--yes",
                    ],
                    print_fn=output.append,
                )

            self.assertEqual(code, 2)
            council.assert_not_awaited()
            self.assertIn("exceeds --max-cost-usd", "\n".join(output))

    def test_consent_defaults_to_rejection_before_model_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feature_file = self._feature_file(root)
            context = self._context(str(root / "target"))
            generalist = AsyncMock()
            output: list[str] = []

            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", return_value=context),
                patch("council.cli.run_generalist", generalist),
            ):
                code = main(
                    [
                        "run",
                        "--repo",
                        context.repository_path,
                        "--feature-file",
                        str(feature_file),
                        "--mode",
                        "generalist",
                    ],
                    input_fn=lambda _prompt: "",
                    print_fn=output.append,
                )

            self.assertEqual(code, 2)
            generalist.assert_not_awaited()
            rendered = "\n".join(output)
            self.assertIn("Repository-derived context", rendered)
            self.assertIn("consent was not granted", rendered)
            self.assertNotIn(MODEL_ENVIRONMENT["OPENAI_API_KEY"], rendered)

    def test_successful_mocked_council_run_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            feature_file = self._feature_file(root)
            output_root = root / "runs"
            context = self._context(str(target))
            execution = self._council_execution(context)
            output: list[str] = []

            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", return_value=context),
                patch(
                    "council.cli.run_council_with_telemetry",
                    AsyncMock(return_value=execution),
                ),
            ):
                code = main(
                    self._run_arguments(
                        context,
                        feature_file,
                        output_root,
                        "council",
                    ),
                    print_fn=output.append,
                )

            self.assertEqual(code, 0)
            run_directory = self._only_run_directory(output_root)
            self.assertEqual(
                {path.name for path in run_directory.iterdir()},
                EXPECTED_ARTIFACT_FILES,
            )
            rendered = "\n".join(output)
            self.assertIn("Director decision: PROTOTYPE_FIRST", rendered)
            self.assertIn("Council model calls: 6", rendered)
            self.assertIn(f"Report: {run_directory / 'report.md'}", rendered)
            self.assertIn(f"Run directory: {run_directory}", rendered)
            self.assertNotIn(MODEL_ENVIRONMENT["OPENAI_API_KEY"], rendered)
            for artifact in run_directory.iterdir():
                self.assertNotIn(
                    MODEL_ENVIRONMENT["OPENAI_API_KEY"].encode("utf-8"),
                    artifact.read_bytes(),
                )

    def test_successful_mocked_generalist_run_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            feature_file = self._feature_file(root)
            output_root = root / "runs"
            context = self._context(str(target))
            execution = self._generalist_execution(context)
            output: list[str] = []

            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", return_value=context),
                patch(
                    "council.cli.run_generalist",
                    AsyncMock(return_value=execution),
                ),
            ):
                code = main(
                    self._run_arguments(
                        context,
                        feature_file,
                        output_root,
                        "generalist",
                    ),
                    print_fn=output.append,
                )

            self.assertEqual(code, 0)
            run_directory = self._only_run_directory(output_root)
            self.assertEqual(
                {path.name for path in run_directory.iterdir()},
                EXPECTED_GENERALIST_ARTIFACT_FILES,
            )
            rendered = "\n".join(output)
            self.assertIn("Generalist decision: PROTOTYPE_FIRST", rendered)
            self.assertIn("Council model calls: 0", rendered)
            self.assertIn("Generalist model calls: 1", rendered)
            self.assertIn(f"Report: {run_directory / 'report.md'}", rendered)

    def test_successful_both_reuses_one_context_and_writes_comparison(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            feature_file = self._feature_file(root)
            output_root = root / "runs"
            context = self._context(str(target))
            seen_contexts: list[ContextBundle] = []

            async def council_run(
                feature: str,
                supplied_context: ContextBundle,
            ) -> CouncilExecution:
                self.assertEqual(feature, FEATURE)
                seen_contexts.append(supplied_context)
                return self._council_execution(supplied_context)

            async def generalist_run(
                feature: str,
                supplied_context: ContextBundle,
            ) -> GeneralistExecution:
                self.assertEqual(feature, FEATURE)
                seen_contexts.append(supplied_context)
                return self._generalist_execution(supplied_context)

            output: list[str] = []
            build = Mock(return_value=context)
            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", build),
                patch(
                    "council.cli.run_council_with_telemetry",
                    council_run,
                ),
                patch("council.cli.run_generalist", generalist_run),
            ):
                code = main(
                    self._run_arguments(
                        context,
                        feature_file,
                        output_root,
                        "both",
                    ),
                    print_fn=output.append,
                )

            self.assertEqual(code, 0)
            build.assert_called_once()
            self.assertEqual(seen_contexts, [context, context])
            self.assertIs(seen_contexts[0], seen_contexts[1])
            run_directory = self._only_run_directory(output_root)
            self.assertEqual(
                {path.name for path in run_directory.iterdir()},
                EXPECTED_EVALUATION_ARTIFACT_FILES,
            )
            rendered = "\n".join(output)
            self.assertIn("Expected nominal model calls: 7", rendered)
            self.assertIn("Council model calls: 6", rendered)
            self.assertIn("Generalist model calls: 1", rendered)
            self.assertIn("Human comparison status: Pending", rendered)

    def test_orchestration_failure_is_nonzero_and_not_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            feature_file = self._feature_file(root)
            output_root = root / "runs"
            context = self._context(str(target))
            output: list[str] = []

            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", return_value=context),
                patch(
                    "council.cli.run_council_with_telemetry",
                    AsyncMock(
                        side_effect=CouncilOrchestrationError(
                            "synthetic failure"
                        )
                    ),
                ),
            ):
                code = main(
                    self._run_arguments(
                        context,
                        feature_file,
                        output_root,
                        "council",
                    ),
                    print_fn=output.append,
                )

            self.assertEqual(code, 2)
            self.assertEqual(list(output_root.iterdir()), [])
            rendered = "\n".join(output)
            self.assertIn("synthetic failure", rendered)
            self.assertNotIn("Run complete", rendered)

    def test_wrapped_orchestration_error_redacts_api_key_everywhere(
        self,
    ) -> None:
        secret = MODEL_ENVIRONMENT["OPENAI_API_KEY"]
        error = CouncilOrchestrationError(
            f"Authorization: Bearer {secret}; repeated={secret}"
        )

        code, captured = self._run_council_failure(error)

        self.assertEqual(code, 2)
        self.assertNotIn(secret, captured)
        self.assertEqual(captured.count("[REDACTED]"), 2)
        self.assertIn("Authorization: Bearer [REDACTED]", captured)
        self.assertNotIn("Run complete", captured)

    def test_unexpected_provider_error_redacts_api_key_everywhere(self) -> None:
        secret = MODEL_ENVIRONMENT["OPENAI_API_KEY"]
        error = RuntimeError(
            f"SDK request failed at https://provider.test/?api_key={secret}"
        )

        code, captured = self._run_council_failure(error)

        self.assertEqual(code, 2)
        self.assertNotIn(secret, captured)
        self.assertIn("[REDACTED]", captured)
        self.assertIn("SDK request failed", captured)
        self.assertNotIn("Run complete", captured)

    def test_rich_error_boundary_redacts_api_key(self) -> None:
        secret = MODEL_ENVIRONMENT["OPENAI_API_KEY"]
        context = self._context("/synthetic/repository")
        runner = CliRunner()
        error = CouncilOrchestrationError(
            f"Authorization: Bearer {secret}; repeated={secret}"
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", return_value=context),
                patch(
                    "council.cli.run_council_with_telemetry",
                    AsyncMock(side_effect=error),
                ),
            ):
                result = runner.invoke(
                    app,
                    [
                        "run",
                        "--repo",
                        context.repository_path,
                        "--feature",
                        "Direct feature",
                        "--mode",
                        "council",
                        "--yes",
                        "--output-dir",
                        temporary_directory,
                    ],
                )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertNotIn(secret, result.output)
        self.assertEqual(result.output.count("[REDACTED]"), 2)
        self.assertIn("Authorization: Bearer [REDACTED]", result.output)
        self.assertNotIn("Run complete", result.output)

    def test_real_dry_run_does_not_mutate_target_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            self._initialize_repository(target)
            feature_file = self._feature_file(root)
            before = self._worktree_snapshot(target)
            before_status = self._git_status(target)
            output: list[str] = []

            with patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False):
                code = main(
                    [
                        "run",
                        "--repo",
                        str(target),
                        "--feature-file",
                        str(feature_file),
                        "--dry-run",
                    ],
                    print_fn=output.append,
                )

            self.assertEqual(code, 0)
            self.assertEqual(self._worktree_snapshot(target), before)
            self.assertEqual(self._git_status(target), before_status)
            self.assertFalse((target / "runs").exists())

    def test_missing_model_configuration_and_api_key_are_not_leaked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            feature_file = self._feature_file(root)
            context = self._context(str(root / "target"))

            for environment, expected in (
                (
                    {
                        "COUNCIL_SPECIALIST_MODEL": "",
                        "COUNCIL_SYNTHESIS_MODEL": "",
                        "OPENAI_API_KEY": "",
                    },
                    "Missing model configuration",
                ),
                (
                    {
                        "COUNCIL_SPECIALIST_MODEL": "gpt-5.4-nano",
                        "COUNCIL_SYNTHESIS_MODEL": "gpt-5.4-nano",
                        "OPENAI_API_KEY": "",
                    },
                    "OPENAI_API_KEY is required",
                ),
            ):
                with self.subTest(expected=expected):
                    output: list[str] = []
                    with (
                        patch.dict(os.environ, environment, clear=False),
                        patch(
                            "council.cli.build_context",
                            return_value=context,
                        ),
                    ):
                        code = main(
                            [
                                "run",
                                "--repo",
                                context.repository_path,
                                "--feature-file",
                                str(feature_file),
                                "--yes",
                            ],
                            print_fn=output.append,
                        )

                    self.assertEqual(code, 2)
                    rendered = "\n".join(output)
                    self.assertIn(expected, rendered)
                    self.assertNotIn("synthetic-secret-key", rendered)

    def _context(self, repository_path: str) -> ContextBundle:
        evidence = RepositoryEvidence(
            id="repo-001",
            file_path="src/ExampleFeatureService.py",
            selection_reasons=["feature term match", "source relevance"],
            matched_terms=["repeat", "engagement"],
            text="Synthetic repository excerpt.\n",
            truncated=False,
        )
        return ContextBundle(
            repository_path=repository_path,
            commit_sha="4" * 40,
            branch="example-branch",
            working_tree_dirty=True,
            feature_input=FEATURE,
            search_terms=["repeat", "engagement", "experiment"],
            configuration=ContextBuilderConfig(),
            tracked_file_count=3,
            candidate_file_count=2,
            selected_file_count=1,
            skipped_file_count=0,
            total_text_characters=len(evidence.text),
            truncated_file_count=0,
            selection_limited=False,
            evidence=[evidence],
        )

    def _run_council_failure(self, error: Exception) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            feature_file = self._feature_file(root)
            output_root = root / "runs"
            context = self._context(str(target))
            output: list[str] = []
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", return_value=context),
                patch(
                    "council.cli.run_council_with_telemetry",
                    AsyncMock(side_effect=error),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = main(
                    self._run_arguments(
                        context,
                        feature_file,
                        output_root,
                        "council",
                    ),
                    print_fn=output.append,
                )

            self.assertEqual(list(output_root.iterdir()), [])
            captured = "\n".join(output) + stdout.getvalue() + stderr.getvalue()
            return code, captured

    def _council_execution(
        self,
        context: ContextBundle,
    ) -> CouncilExecution:
        roles = {
            role: RoleTelemetry(
                model="gpt-5.4-nano",
                duration_ms=float(index * 10),
                usage=TokenUsage(
                    requests=1,
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                ),
            )
            for index, role in enumerate(ROLE_NAMES, start=1)
        }
        return CouncilExecution(
            result=CouncilResult(
                context=context,
                game_design=GAME_DESIGN,
                technical=TECHNICAL,
                analytics=ANALYTICS,
                scope_risk=SCOPE_RISK,
                producer=PRODUCER,
                director=DIRECTOR,
            ),
            telemetry=CouncilTelemetry(
                started_at=STARTED_AT,
                total_duration_ms=180,
                roles=roles,
                total_usage=TokenUsage(
                    requests=6,
                    input_tokens=600,
                    output_tokens=300,
                    total_tokens=900,
                ),
            ),
        )

    def _generalist_execution(
        self,
        context: ContextBundle,
    ) -> GeneralistExecution:
        return GeneralistExecution(
            feature_sha256=self._feature_sha256(),
            context_identity=build_context_identity(context),
            result=DIRECTOR,
            telemetry=RoleTelemetry(
                model="gpt-5.4-nano",
                duration_ms=75,
                usage=TokenUsage(
                    requests=1,
                    input_tokens=1_000,
                    output_tokens=200,
                    total_tokens=1_200,
                ),
            ),
            estimated_cost_usd=Decimal("0.000450"),
            unpriced_models=[],
            pricing_snapshot_id="openai-api-pricing-2026-08-28",
        )

    def _feature_sha256(self) -> str:
        import hashlib

        return hashlib.sha256(FEATURE.encode("utf-8")).hexdigest()

    def _feature_file(self, root: Path) -> Path:
        path = root / "feature.txt"
        path.write_text(FEATURE, encoding="utf-8")
        return path

    def _run_arguments(
        self,
        context: ContextBundle,
        feature_file: Path,
        output_root: Path,
        mode: str,
    ) -> list[str]:
        return [
            "run",
            "--repo",
            context.repository_path,
            "--feature-file",
            str(feature_file),
            "--mode",
            mode,
            "--yes",
            "--output-dir",
            str(output_root),
        ]

    def _only_run_directory(self, output_root: Path) -> Path:
        run_directories = list(output_root.iterdir())
        self.assertEqual(len(run_directories), 1)
        return run_directories[0]

    def _initialize_repository(self, repository: Path) -> None:
        (repository / "README.md").write_text(
            "Synthetic engagement feature repository.\n",
            encoding="utf-8",
        )
        self._git(repository, "init")
        self._git(repository, "config", "user.email", "cli@example.test")
        self._git(repository, "config", "user.name", "CLI Test")
        self._git(repository, "add", "README.md")
        self._git(repository, "commit", "-m", "initial")

    def _git(self, repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def _git_status(self, repository: Path) -> str:
        return self._git(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )

    def _worktree_snapshot(self, repository: Path) -> dict[str, bytes]:
        return {
            path.relative_to(repository).as_posix(): path.read_bytes()
            for path in repository.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }


if __name__ == "__main__":
    unittest.main()
