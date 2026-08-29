import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from council.cli import main, parse_args
from council.evaluation import build_context_identity, create_comparison_record
from council.models import (
    ComparisonPreference,
    ComparisonRecord,
    ComparisonRubricScores,
    ContextBuilderConfig,
    ContextBundle,
    CouncilExecution,
    CouncilResult,
    CouncilTelemetry,
    DirectorDecision,
    GeneralistExecution,
    HumanAction,
    HumanComparisonReview,
    HumanDecision,
    RepositoryEvidence,
    RoleTelemetry,
    RunRecord,
    TokenUsage,
)
from council.reporting import (
    RunArtifactError,
    create_run_record,
    update_comparison_with_human_review,
    update_run_with_human_decision,
    write_evaluation_artifacts,
    write_generalist_artifacts,
    write_run_artifacts,
)
from test_director_agent import PRODUCER
from test_orchestrator import DIRECTOR
from test_producer_agent import (
    ANALYTICS,
    FEATURE,
    GAME_DESIGN,
    SCOPE_RISK,
    TECHNICAL,
)


RUN_ID = "review-run-001"
STARTED_AT = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
REVIEWED_AT = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)
ROLE_NAMES = (
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
    "producer",
    "director",
)


class ReviewCliTests(unittest.TestCase):
    def test_parser_recognizes_review_and_uses_normal_runs_root(self) -> None:
        args = parse_args(["review", "--run", RUN_ID])

        self.assertEqual(args.command, "review")
        self.assertEqual(args.run, RUN_ID)
        self.assertEqual(args.output_dir, "runs")

    def test_pending_product_review_accept_reject_and_modify(self) -> None:
        cases = (
            (
                "accept",
                ["a", "Accepted after review."],
                HumanAction.ACCEPT,
                DIRECTOR.decision,
            ),
            (
                "reject",
                ["r", "Not suitable for this milestone."],
                HumanAction.REJECT,
                None,
            ),
            (
                "modify",
                ["m", "GO", "Proceed with the bounded alternative."],
                HumanAction.MODIFY,
                DirectorDecision.GO,
            ),
        )

        for name, responses, action, final_decision in cases:
            with self.subTest(action=name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    fixture = self._write_fixture(root)
                    before = self._snapshot(fixture.run_directory)
                    output: list[str] = []

                    code = main(
                        self._review_arguments(fixture.output_root),
                        input_fn=self._input(responses),
                        print_fn=output.append,
                    )

                    self.assertEqual(code, 0)
                    persisted = self._read_run(fixture.run_directory)
                    self.assertEqual(persisted.ai_recommendation, DIRECTOR.decision)
                    self.assertIsNotNone(persisted.human_decision)
                    assert persisted.human_decision is not None
                    self.assertEqual(persisted.human_decision.action, action)
                    self.assertEqual(
                        persisted.human_decision.final_decision,
                        final_decision,
                    )
                    self.assertIn(
                        "Resolved by Human Decision",
                        (fixture.run_directory / "report.md").read_text(
                            encoding="utf-8"
                        ),
                    )
                    self.assertEqual(
                        self._changed_files(before, fixture.run_directory),
                        {"run.json", "report.md"},
                    )
                    rendered = "\n".join(output)
                    self.assertIn(f"Run: {RUN_ID}", rendered)
                    self.assertIn(
                        f"AI recommendation: {DIRECTOR.decision.value}",
                        rendered,
                    )
                    self.assertIn("Human review complete", rendered)
                    self.assertIn("run.json", rendered)
                    self.assertIn("report.md", rendered)
                    self.assertNotIn("comparison.json", rendered)

    def test_completed_product_and_comparison_are_idempotent(self) -> None:
        decision = self._accepted_decision()
        review = self._comparison_review(ComparisonPreference.COUNCIL)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(
                root,
                human_decision=decision,
                with_comparison=True,
                human_review=review,
            )
            before = self._snapshot(fixture.run_directory)
            output: list[str] = []

            with (
                patch(
                    "council.cli.update_run_with_human_decision"
                ) as update_run,
                patch(
                    "council.cli.update_comparison_with_human_review"
                ) as update_comparison,
            ):
                first = main(
                    self._review_arguments(fixture.output_root),
                    input_fn=self._unexpected_input,
                    print_fn=output.append,
                )
                second = main(
                    self._review_arguments(fixture.output_root),
                    input_fn=self._unexpected_input,
                    print_fn=output.append,
                )

            self.assertEqual(first, 0)
            self.assertEqual(second, 0)
            update_run.assert_not_called()
            update_comparison.assert_not_called()
            self.assertEqual(self._snapshot(fixture.run_directory), before)
            rendered = "\n".join(output)
            self.assertIn("Product/Director review: already completed", rendered)
            self.assertIn("Action: accept", rendered)
            self.assertIn(
                "Council comparison review: already completed",
                rendered,
            )
            self.assertIn("Preference: council", rendered)
            self.assertEqual(rendered.count("Updated: none"), 2)

    def test_persistence_helpers_refuse_completed_review_replacement(self) -> None:
        decision = self._accepted_decision()
        review = self._comparison_review(ComparisonPreference.COUNCIL)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(
                root,
                human_decision=decision,
                with_comparison=True,
                human_review=review,
            )
            before = self._snapshot(fixture.run_directory)
            replacement_decision = HumanDecision(
                action=HumanAction.REJECT,
                final_decision=None,
                note="Replacement must not be allowed.",
                timestamp=REVIEWED_AT,
            )
            replacement_review = self._comparison_review(
                ComparisonPreference.GENERALIST
            )

            with self.assertRaisesRegex(RunArtifactError, "already completed"):
                update_run_with_human_decision(
                    fixture.run_directory,
                    replacement_decision,
                )
            with self.assertRaisesRegex(RunArtifactError, "already completed"):
                update_comparison_with_human_review(
                    fixture.run_directory,
                    replacement_review,
                )

            self.assertEqual(self._snapshot(fixture.run_directory), before)

    def test_pending_comparison_accepts_all_scores_and_each_preference(self) -> None:
        generalist_values = (1, 2, 3, 4, 5, 1, 2, 3)
        council_values = (5, 4, 3, 2, 1, 5, 4, 3)
        cases = (
            ("a", ComparisonPreference.GENERALIST),
            ("b", ComparisonPreference.COUNCIL),
            ("t", ComparisonPreference.TIE),
        )

        for choice, expected_preference in cases:
            with self.subTest(preference=expected_preference.value):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    fixture = self._write_fixture(
                        root,
                        human_decision=self._accepted_decision(),
                        with_comparison=True,
                    )
                    original_run = (fixture.run_directory / "run.json").read_bytes()
                    responses = [
                        *(str(value) for value in generalist_values),
                        *(str(value) for value in council_values),
                        choice,
                        f"Reason for {expected_preference.value}.",
                    ]

                    code = main(
                        self._review_arguments(fixture.output_root),
                        input_fn=self._input(responses),
                        print_fn=lambda _line: None,
                    )

                    self.assertEqual(code, 0)
                    comparison = self._read_comparison(fixture.run_directory)
                    self.assertIsNotNone(comparison.human_review)
                    assert comparison.human_review is not None
                    fields = tuple(ComparisonRubricScores.model_fields)
                    self.assertEqual(
                        tuple(
                            getattr(comparison.human_review.generalist_scores, field)
                            for field in fields
                        ),
                        generalist_values,
                    )
                    self.assertEqual(
                        tuple(
                            getattr(comparison.human_review.council_scores, field)
                            for field in fields
                        ),
                        council_values,
                    )
                    self.assertEqual(
                        comparison.human_review.preference,
                        expected_preference,
                    )
                    self.assertEqual(
                        (fixture.run_directory / "run.json").read_bytes(),
                        original_run,
                    )
                    report = (fixture.run_directory / "report.md").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn("### Human Rubric", report)
                    self.assertIn(
                        f"Human preference: **{expected_preference.value}**",
                        report,
                    )

    def test_comparison_prompt_retries_invalid_scores_preference_and_reason(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(
                root,
                human_decision=self._accepted_decision(),
                with_comparison=True,
            )
            responses = [
                "not-an-integer",
                "0",
                "5",
                *("4" for _ in range(7)),
                "6",
                "3",
                *("3" for _ in range(7)),
                "invalid",
                "t",
                "",
                "   ",
                "Both paths contribute different useful constraints.",
            ]
            output: list[str] = []

            code = main(
                self._review_arguments(fixture.output_root),
                input_fn=self._input(responses),
                print_fn=output.append,
            )

            self.assertEqual(code, 0)
            comparison = self._read_comparison(fixture.run_directory)
            assert comparison.human_review is not None
            self.assertEqual(
                comparison.human_review.preference,
                ComparisonPreference.TIE,
            )
            self.assertEqual(
                comparison.human_review.reason,
                "Both paths contribute different useful constraints.",
            )
            rendered = "\n".join(output)
            self.assertGreaterEqual(rendered.count("integer from 1 to 5"), 3)
            self.assertIn("Enter A, B, or T", rendered)
            self.assertGreaterEqual(rendered.count("reason is required"), 2)

    def test_council_only_run_without_comparison_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root)
            output: list[str] = []

            code = main(
                self._review_arguments(fixture.output_root),
                input_fn=self._input(["a", ""]),
                print_fn=output.append,
            )

            self.assertEqual(code, 0)
            self.assertIsNotNone(self._read_run(fixture.run_directory).human_decision)
            self.assertFalse((fixture.run_directory / "comparison.json").exists())
            self.assertIn(
                "Council-vs-Generalist comparison: not available for this run",
                "\n".join(output),
            )

    def test_generalist_only_run_is_reported_without_inventing_human_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root, generalist_only=True)
            before = self._snapshot(fixture.run_directory)
            output: list[str] = []

            code = main(
                self._review_arguments(fixture.output_root),
                input_fn=self._unexpected_input,
                print_fn=output.append,
            )

            self.assertEqual(code, 0)
            self.assertEqual(self._snapshot(fixture.run_directory), before)
            rendered = "\n".join(output)
            self.assertIn(f"AI recommendation: {DIRECTOR.decision.value}", rendered)
            self.assertIn("Product/Director review: Not available", rendered)
            self.assertIn(
                "Council vs Generalist: not available for this run",
                rendered,
            )
            self.assertIn("Human review complete", rendered)
            self.assertIn("Updated: none", rendered)

    def test_review_needs_no_credentials_models_context_or_model_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root)
            output: list[str] = []

            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "council.cli._read_model_configuration",
                    side_effect=AssertionError("model config must not be read"),
                ) as read_models,
                patch(
                    "council.cli.build_context",
                    side_effect=AssertionError("context must not be rebuilt"),
                ) as build_context,
                patch(
                    "council.cli.run_council_with_telemetry",
                    new=AsyncMock(
                        side_effect=AssertionError("Council must not execute")
                    ),
                ) as run_council,
                patch(
                    "council.cli.run_generalist",
                    new=AsyncMock(
                        side_effect=AssertionError("Generalist must not execute")
                    ),
                ) as run_generalist,
            ):
                code = main(
                    self._review_arguments(fixture.output_root),
                    input_fn=self._input(["a", "offline review"]),
                    print_fn=output.append,
                )

            self.assertEqual(code, 0)
            read_models.assert_not_called()
            build_context.assert_not_called()
            run_council.assert_not_awaited()
            run_generalist.assert_not_awaited()
            self.assertNotIn("OPENAI_API_KEY", "\n".join(output))

    def test_review_entry_points_work_when_agents_cannot_be_imported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root, with_comparison=True)
            environment, import_marker = self._blocked_agents_environment(root)
            repository_root = Path(__file__).resolve().parent

            for arguments in (("--help",), ("review", "--help")):
                with self.subTest(arguments=arguments):
                    completed = subprocess.run(
                        [sys.executable, "-m", "council", *arguments],
                        cwd=repository_root,
                        env=environment,
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        check=False,
                    )
                    captured = completed.stdout + completed.stderr
                    self.assertEqual(completed.returncode, 0, captured)
                    self.assertIn("Usage:", captured)
                    self.assertNotIn("Traceback", captured)
                    self.assertFalse(import_marker.exists())

            responses = [
                "a",
                "Approved during an isolated offline review.",
                *("3" for _ in range(8)),
                *("4" for _ in range(8)),
                "b",
                "Council exposed more actionable constraints.",
            ]
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "council",
                    "review",
                    "--run",
                    RUN_ID,
                    "--output-dir",
                    str(fixture.output_root),
                ],
                cwd=repository_root,
                env=environment,
                input="\n".join(responses) + "\n",
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            captured = completed.stdout + completed.stderr

            self.assertEqual(completed.returncode, 0, captured)
            self.assertIn("Human review complete", captured)
            self.assertNotIn("Traceback", captured)
            self.assertFalse(import_marker.exists())
            self.assertIsNotNone(
                self._read_run(fixture.run_directory).human_decision
            )
            self.assertIsNotNone(
                self._read_comparison(fixture.run_directory).human_review
            )

            run_environment = environment.copy()
            run_environment.pop("COUNCIL_TEST_BLOCK_CONTEXT", None)
            run_environment.update(
                {
                    "COUNCIL_SPECIALIST_MODEL": "gpt-5.4-nano",
                    "COUNCIL_SYNTHESIS_MODEL": "gpt-5.4-nano",
                }
            )
            feature_file = root / "feature.txt"
            feature_file.write_text(FEATURE, encoding="utf-8")
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "council",
                    "run",
                    "--repo",
                    str(fixture.target_repository),
                    "--feature-file",
                    str(feature_file),
                    "--dry-run",
                ],
                cwd=repository_root,
                env=run_environment,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            run_output = run.stdout + run.stderr

            self.assertNotEqual(run.returncode, 0)
            self.assertTrue(import_marker.exists())
            self.assertIn("openai-agents", run_output)
            self.assertIn("before any API calls", run_output)
            self.assertNotIn("Traceback", run_output)

    def test_only_human_review_artifacts_change_and_target_is_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root, with_comparison=True)
            target_file = fixture.target_repository / "sentinel.txt"
            target_file.write_text("must remain unchanged\n", encoding="utf-8")
            run_before = self._snapshot(fixture.run_directory)
            target_before = self._snapshot(fixture.target_repository)
            output: list[str] = []
            responses = [
                "a",
                "Approved by product.",
                *("3" for _ in range(8)),
                *("4" for _ in range(8)),
                "b",
                "Council was more actionable.",
            ]

            with patch(
                "council.cli.build_context",
                side_effect=AssertionError("target repository must not be inspected"),
            ) as build_context:
                code = main(
                    self._review_arguments(fixture.output_root),
                    input_fn=self._input(responses),
                    print_fn=output.append,
                )

            self.assertEqual(code, 0)
            build_context.assert_not_called()
            self.assertEqual(
                self._changed_files(run_before, fixture.run_directory),
                {"run.json", "comparison.json", "report.md"},
            )
            self.assertEqual(self._snapshot(fixture.target_repository), target_before)
            rendered = "\n".join(output)
            self.assertIn("Updated:", rendered)
            for filename in ("run.json", "comparison.json", "report.md"):
                self.assertIn(filename, rendered)
            for filename in (
                "context.json",
                "game_design.json",
                "technical.json",
                "analytics.json",
                "scope_risk.json",
                "producer.json",
                "director.json",
                "generalist.json",
                "input.json",
            ):
                self.assertNotIn(f"Updated: {filename}", rendered)

    def test_unsafe_and_missing_run_ids_are_rejected_without_reads_or_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_root = root / "runs"
            output_root.mkdir()
            outside = root / "outside.json"
            outside.write_text("sentinel\n", encoding="utf-8")
            before = self._snapshot(root)

            for run_id in (
                "../outside",
                "..\\outside",
                "nested/run",
                "review-run-001.",
                "CON",
                str((root / "absolute-run").resolve()),
            ):
                with self.subTest(run_id=run_id):
                    output: list[str] = []
                    code = main(
                        [
                            "review",
                            "--run",
                            run_id,
                            "--output-dir",
                            str(output_root),
                        ],
                        input_fn=self._unexpected_input,
                        print_fn=output.append,
                    )
                    self.assertNotEqual(code, 0)
                    self.assertIn("Unsafe run ID", "\n".join(output))

            missing_output: list[str] = []
            missing_code = main(
                [
                    "review",
                    "--run",
                    "missing-run",
                    "--output-dir",
                    str(output_root),
                ],
                input_fn=self._unexpected_input,
                print_fn=missing_output.append,
            )
            self.assertNotEqual(missing_code, 0)
            self.assertIn("not", "\n".join(missing_output).lower())
            self.assertEqual(self._snapshot(root), before)

    def test_missing_and_corrupt_run_records_fail_before_prompting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_root = root / "runs"
            output_root.mkdir()
            run_directory = output_root / RUN_ID
            run_directory.mkdir()

            missing_output: list[str] = []
            missing_code = main(
                self._review_arguments(output_root),
                input_fn=self._unexpected_input,
                print_fn=missing_output.append,
            )
            self.assertNotEqual(missing_code, 0)
            self.assertIn("run.json", "\n".join(missing_output))

            (run_directory / "run.json").write_text("{not-json", encoding="utf-8")
            before = self._snapshot(run_directory)
            corrupt_output: list[str] = []
            corrupt_code = main(
                self._review_arguments(output_root),
                input_fn=self._unexpected_input,
                print_fn=corrupt_output.append,
            )
            self.assertNotEqual(corrupt_code, 0)
            self.assertIn("run.json", "\n".join(corrupt_output))
            self.assertEqual(self._snapshot(run_directory), before)

    def test_corrupt_comparison_prevents_product_prompt_and_all_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root, with_comparison=True)
            (fixture.run_directory / "comparison.json").write_text(
                "{not-json",
                encoding="utf-8",
            )
            before = self._snapshot(fixture.run_directory)
            output: list[str] = []

            with patch(
                "council.cli.update_run_with_human_decision"
            ) as update_run:
                code = main(
                    self._review_arguments(fixture.output_root),
                    input_fn=self._unexpected_input,
                    print_fn=output.append,
                )

            self.assertNotEqual(code, 0)
            update_run.assert_not_called()
            self.assertEqual(self._snapshot(fixture.run_directory), before)
            self.assertIn("comparison.json", "\n".join(output))
            self.assertNotIn("Human review complete", "\n".join(output))

    def test_mismatched_comparison_identity_fails_before_prompt_or_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root, with_comparison=True)
            comparison_path = fixture.run_directory / "comparison.json"
            payload = json.loads(comparison_path.read_text(encoding="utf-8"))
            payload["feature_sha256"] = "0" * 64
            comparison_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            before = self._snapshot(fixture.run_directory)
            output: list[str] = []

            code = main(
                self._review_arguments(fixture.output_root),
                input_fn=self._unexpected_input,
                print_fn=output.append,
            )

            self.assertNotEqual(code, 0)
            self.assertEqual(self._snapshot(fixture.run_directory), before)
            self.assertIn("identity", "\n".join(output).lower())
            self.assertNotIn("Human review complete", "\n".join(output))

    def test_malformed_existing_human_decision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root)
            run_path = fixture.run_directory / "run.json"
            payload = json.loads(run_path.read_text(encoding="utf-8"))
            payload["human_decision"] = {
                "action": "accept",
                "final_decision": "GO",
                "note": "This contradicts the AI recommendation.",
                "timestamp": REVIEWED_AT.isoformat(),
            }
            run_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            before = self._snapshot(fixture.run_directory)
            output: list[str] = []

            code = main(
                self._review_arguments(fixture.output_root),
                input_fn=self._unexpected_input,
                print_fn=output.append,
            )

            self.assertNotEqual(code, 0)
            self.assertEqual(self._snapshot(fixture.run_directory), before)
            self.assertIn("accepted human decision", "\n".join(output).lower())
            self.assertNotIn("Human review complete", "\n".join(output))

    def test_persistence_failure_returns_nonzero_without_false_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = self._write_fixture(root)
            output: list[str] = []

            with patch(
                "council.cli.update_run_with_human_decision",
                side_effect=OSError("synthetic persistence failure"),
            ):
                code = main(
                    self._review_arguments(fixture.output_root),
                    input_fn=self._input(["a", ""]),
                    print_fn=output.append,
                )

            self.assertNotEqual(code, 0)
            rendered = "\n".join(output)
            self.assertIn("synthetic persistence failure", rendered)
            self.assertNotIn("Human review complete", rendered)
            self.assertNotIn("Updated:", rendered)

    def _write_fixture(
        self,
        root: Path,
        *,
        human_decision: HumanDecision | None = None,
        with_comparison: bool = False,
        human_review: HumanComparisonReview | None = None,
        generalist_only: bool = False,
    ) -> "_ReviewFixture":
        target_repository = root / "target-repository"
        target_repository.mkdir()
        output_root = root / "runs"
        output_root.mkdir()
        context = self._context(str(target_repository))
        generalist = self._generalist_execution(context)

        if generalist_only:
            run_directory = write_generalist_artifacts(
                RUN_ID,
                FEATURE,
                context,
                generalist,
                output_root,
                started_at=STARTED_AT,
            )
            return _ReviewFixture(
                output_root=output_root,
                run_directory=run_directory,
                target_repository=target_repository,
            )

        record = create_run_record(
            FEATURE,
            self._council_execution(context),
            run_id=RUN_ID,
        )
        if human_decision is not None:
            record = record.model_copy(update={"human_decision": human_decision})
        run_directory = write_run_artifacts(record, output_root)
        if with_comparison:
            comparison = create_comparison_record(record, generalist)
            if human_review is not None:
                comparison = comparison.model_copy(
                    update={"human_review": human_review}
                )
            write_evaluation_artifacts(run_directory, comparison)
        return _ReviewFixture(
            output_root=output_root,
            run_directory=run_directory,
            target_repository=target_repository,
        )

    def _blocked_agents_environment(
        self,
        root: Path,
    ) -> tuple[dict[str, str], Path]:
        blocker_directory = root / "import-blocker"
        blocker_directory.mkdir()
        import_marker = root / "agents-import-attempted.txt"
        sitecustomize = blocker_directory / "sitecustomize.py"
        sitecustomize.write_text(
            """import importlib.abc
import os
import sys
from pathlib import Path


class _BlockRunDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = fullname == "agents" or fullname.startswith("agents.")
        blocked = blocked or (
            fullname == "council.context"
            and os.environ.get("COUNCIL_TEST_BLOCK_CONTEXT") == "1"
        )
        if blocked:
            marker = os.environ.get("COUNCIL_TEST_AGENTS_IMPORT_MARKER")
            if marker:
                Path(marker).write_text(fullname, encoding="utf-8")
            raise ModuleNotFoundError(
                "third-party agents package blocked by test",
                name=fullname,
            )
        return None


sys.meta_path.insert(0, _BlockRunDependencies())
""",
            encoding="utf-8",
        )
        repository_root = str(Path(__file__).resolve().parent)
        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            path
            for path in (
                str(blocker_directory),
                repository_root,
                existing_pythonpath,
            )
            if path
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["COUNCIL_TEST_AGENTS_IMPORT_MARKER"] = str(import_marker)
        environment["COUNCIL_TEST_BLOCK_CONTEXT"] = "1"
        for name in (
            "OPENAI_API_KEY",
            "COUNCIL_SPECIALIST_MODEL",
            "COUNCIL_SYNTHESIS_MODEL",
        ):
            environment.pop(name, None)
        return environment, import_marker

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
            commit_sha="7" * 40,
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

    def _council_execution(self, context: ContextBundle) -> CouncilExecution:
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
            feature_sha256=hashlib.sha256(FEATURE.encode("utf-8")).hexdigest(),
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

    def _accepted_decision(self) -> HumanDecision:
        return HumanDecision(
            action=HumanAction.ACCEPT,
            final_decision=DIRECTOR.decision,
            note="Accepted in an earlier review.",
            timestamp=REVIEWED_AT,
        )

    def _comparison_review(
        self,
        preference: ComparisonPreference,
    ) -> HumanComparisonReview:
        return HumanComparisonReview(
            generalist_scores=self._scores(3),
            council_scores=self._scores(4),
            preference=preference,
            reason="Council exposed more actionable constraints.",
            insights=[],
            timestamp=REVIEWED_AT,
        )

    def _scores(self, value: int) -> ComparisonRubricScores:
        return ComparisonRubricScores(
            grounding=value,
            scope_reduction=value,
            hypothesis_quality=value,
            experiment_credibility=value,
            measurement_to_learning_logic=value,
            technical_realism=value,
            decision_usefulness=value,
            conciseness=value,
        )

    def _review_arguments(self, output_root: Path) -> list[str]:
        return [
            "review",
            "--run",
            RUN_ID,
            "--output-dir",
            str(output_root),
        ]

    def _input(self, responses: list[str]):
        response_iterator = iter(responses)

        def read(_prompt: str) -> str:
            try:
                return next(response_iterator)
            except StopIteration as error:
                raise AssertionError(
                    "review prompted more times than expected"
                ) from error

        return read

    def _unexpected_input(self, prompt: str) -> str:
        raise AssertionError(f"review unexpectedly prompted: {prompt}")

    def _read_run(self, run_directory: Path) -> RunRecord:
        return RunRecord.model_validate_json(
            (run_directory / "run.json").read_text(encoding="utf-8")
        )

    def _read_comparison(self, run_directory: Path) -> ComparisonRecord:
        return ComparisonRecord.model_validate_json(
            (run_directory / "comparison.json").read_text(encoding="utf-8")
        )

    def _snapshot(self, directory: Path) -> dict[str, bytes]:
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    def _changed_files(
        self,
        before: dict[str, bytes],
        directory: Path,
    ) -> set[str]:
        after = self._snapshot(directory)
        return {
            name
            for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)
        }


class _ReviewFixture:
    def __init__(
        self,
        *,
        output_root: Path,
        run_directory: Path,
        target_repository: Path,
    ) -> None:
        self.output_root = output_root
        self.run_directory = run_directory
        self.target_repository = target_repository


if __name__ == "__main__":
    unittest.main()
