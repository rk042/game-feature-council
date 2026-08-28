import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from council.models import (
    ContextBuilderConfig,
    ContextBundle,
    CouncilExecution,
    CouncilResult,
    CouncilTelemetry,
    DirectorDecision,
    HumanAction,
    HumanDecision,
    RepositoryEvidence,
    RoleTelemetry,
    TokenUsage,
)
from council.pricing import estimate_cost
from council.reporting import (
    EXPECTED_ARTIFACT_FILES,
    RunArtifactError,
    create_run_record,
    prompt_for_human_decision,
    update_run_with_human_decision,
    write_run_artifacts,
)
from test_orchestrator import DIRECTOR
from test_producer_agent import (
    ANALYTICS,
    FEATURE,
    GAME_DESIGN,
    SCOPE_RISK,
    TECHNICAL,
)
from test_director_agent import PRODUCER


STARTED_AT = datetime(2026, 8, 28, 10, 30, tzinfo=timezone.utc)
HUMAN_TIMESTAMP = datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc)
ROLE_NAMES = (
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
    "producer",
    "director",
)


class ReportingTests(unittest.TestCase):
    def test_run_record_retains_provenance_telemetry_and_ai_decision(self) -> None:
        execution = self._execution("/synthetic/example-repository")

        record = create_run_record(
            FEATURE,
            execution,
            run_id="run-001",
        )

        self.assertEqual(record.repository_commit_sha, "2" * 40)
        self.assertEqual(record.repository_branch, "example-branch")
        self.assertTrue(record.working_tree_dirty)
        self.assertEqual(record.ai_recommendation, DIRECTOR.decision)
        self.assertIsNone(record.human_decision)
        self.assertEqual(record.telemetry.started_at, STARTED_AT)
        self.assertEqual(record.telemetry.total_duration_ms, 175.5)
        self.assertEqual(set(record.telemetry.roles), set(ROLE_NAMES))
        self.assertEqual(record.telemetry.roles["technical"].duration_ms, 20.0)
        self.assertEqual(record.telemetry.total_usage.input_tokens, 600)
        self.assertEqual(record.telemetry.total_usage.output_tokens, 300)
        self.assertEqual(record.telemetry.total_usage.total_tokens, 900)
        self.assertEqual(record.estimated_cost_usd, Decimal("0.000495"))

    def test_pricing_known_multiple_roles_and_unknown_models(self) -> None:
        million_tokens = TokenUsage(
            requests=1,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            total_tokens=2_000_000,
        )
        known = estimate_cost(
            {
                "one": RoleTelemetry(
                    model="gpt-5.4-nano",
                    duration_ms=1,
                    usage=million_tokens,
                )
            }
        )
        self.assertEqual(known.estimated_cost_usd, Decimal("1.45"))
        self.assertEqual(known.unpriced_models, [])

        half_tokens = TokenUsage(
            input_tokens=500_000,
            output_tokens=500_000,
            total_tokens=1_000_000,
        )
        multiple = estimate_cost(
            {
                role: RoleTelemetry(
                    model="gpt-5.4-nano",
                    duration_ms=1,
                    usage=half_tokens,
                )
                for role in ("one", "two")
            }
        )
        self.assertEqual(multiple.estimated_cost_usd, Decimal("1.450"))

        unknown = estimate_cost(
            {
                "one": RoleTelemetry(
                    model="unpriced-model",
                    duration_ms=1,
                    usage=million_tokens,
                )
            }
        )
        self.assertIsNone(unknown.estimated_cost_usd)
        self.assertEqual(unknown.unpriced_models, ["unpriced-model"])

    def test_artifact_writer_creates_exact_files_and_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target_repository = root / "target-repository"
            target_repository.mkdir()
            target_file = target_repository / "ExampleFeatureService.py"
            target_file.write_text("original content\n", encoding="utf-8")
            output_root = root / "runs"
            output_root.mkdir()
            before = self._directory_contents(target_repository)
            record = create_run_record(
                FEATURE,
                self._execution(str(target_repository)),
                run_id="run-001",
            )

            run_directory = write_run_artifacts(record, output_root)

            self.assertEqual(
                {path.name for path in run_directory.iterdir()},
                EXPECTED_ARTIFACT_FILES,
            )
            for path in run_directory.glob("*.json"):
                json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(self._directory_contents(target_repository), before)

            report = (run_directory / "report.md").read_text(encoding="utf-8")
            for heading in (
                "# Game Feature Council Report",
                "## Director Recommendation",
                "## Proposed Experiment",
                "## Evidence / Grounding",
                "## Runtime",
                "## Human Decision",
            ):
                self.assertIn(heading, report)
            self.assertNotIn("PRIVATE SOURCE EXCERPT", report)
            self.assertIn(
                "PRIVATE SOURCE EXCERPT",
                (run_directory / "context.json").read_text(encoding="utf-8"),
            )

            with self.assertRaisesRegex(
                RunArtifactError,
                "already exists",
            ):
                write_run_artifacts(record, output_root)

    def test_writer_refuses_target_repository_as_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            target_repository = Path(temporary_directory) / "target-repository"
            target_repository.mkdir()
            record = create_run_record(
                FEATURE,
                self._execution(str(target_repository)),
                run_id="run-001",
            )

            with self.assertRaisesRegex(
                RunArtifactError,
                "inside the target repository",
            ):
                write_run_artifacts(record, target_repository)

            self.assertEqual(list(target_repository.iterdir()), [])

    def test_human_prompt_accept_reject_modify_and_invalid_input(self) -> None:
        printed: list[str] = []
        accept = self._prompt(
            ["invalid", "a", "Accepted after review."],
            printed,
        )
        self.assertEqual(accept.action, HumanAction.ACCEPT)
        self.assertEqual(accept.final_decision, DIRECTOR.decision)
        self.assertEqual(accept.note, "Accepted after review.")
        self.assertTrue(any("Enter A, R, or M" in line for line in printed))

        reject = self._prompt(["r", "Not suitable."], [])
        self.assertEqual(reject.action, HumanAction.REJECT)
        self.assertIsNone(reject.final_decision)

        modify_output: list[str] = []
        modify = self._prompt(
            ["m", "invalid", "PROTOTYPE_FIRST", "GO", "Use smaller scope."],
            modify_output,
        )
        self.assertEqual(modify.action, HumanAction.MODIFY)
        self.assertEqual(modify.final_decision, DirectorDecision.GO)
        self.assertTrue(
            any("allowed DirectorDecision" in line for line in modify_output)
        )
        self.assertTrue(
            any("different from the AI" in line for line in modify_output)
        )

    def test_human_update_rewrites_only_run_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target_repository = root / "target-repository"
            target_repository.mkdir()
            output_root = root / "runs"
            output_root.mkdir()
            record = create_run_record(
                FEATURE,
                self._execution(str(target_repository)),
                run_id="run-001",
            )
            run_directory = write_run_artifacts(record, output_root)
            unchanged_before = {
                path.name: path.read_bytes()
                for path in run_directory.iterdir()
                if path.name not in {"run.json", "report.md"}
            }
            decision = HumanDecision(
                action=HumanAction.MODIFY,
                final_decision=DirectorDecision.GO,
                note="Proceed with the bounded alternative.",
                timestamp=HUMAN_TIMESTAMP,
            )

            updated = update_run_with_human_decision(
                run_directory,
                decision,
            )

            self.assertEqual(updated.ai_recommendation, DIRECTOR.decision)
            self.assertEqual(updated.human_decision, decision)
            persisted = json.loads(
                (run_directory / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["human_decision"]["action"], "modify")
            self.assertEqual(
                persisted["human_decision"]["final_decision"],
                "GO",
            )
            report = (run_directory / "report.md").read_text(encoding="utf-8")
            self.assertIn("- Status: modify", report)
            self.assertIn("- Final decision: GO", report)
            unchanged_after = {
                name: (run_directory / name).read_bytes()
                for name in unchanged_before
            }
            self.assertEqual(unchanged_after, unchanged_before)

    def _execution(self, repository_path: str) -> CouncilExecution:
        evidence = RepositoryEvidence(
            id="repo-001",
            file_path="src/ExampleFeatureService.py",
            selection_reasons=["feature term match", "source relevance"],
            matched_terms=["repeat", "engagement"],
            text="PRIVATE SOURCE EXCERPT",
            truncated=False,
        )
        context = ContextBundle(
            repository_path=repository_path,
            commit_sha="2" * 40,
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
        council_result = CouncilResult(
            context=context,
            game_design=GAME_DESIGN,
            technical=TECHNICAL,
            analytics=ANALYTICS,
            scope_risk=SCOPE_RISK,
            producer=PRODUCER,
            director=DIRECTOR,
        )
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
            result=council_result,
            telemetry=CouncilTelemetry(
                started_at=STARTED_AT,
                total_duration_ms=175.5,
                roles=roles,
                total_usage=TokenUsage(
                    requests=6,
                    input_tokens=600,
                    output_tokens=300,
                    total_tokens=900,
                ),
            ),
        )

    def _prompt(
        self,
        responses: list[str],
        printed: list[str],
    ) -> HumanDecision:
        response_iterator = iter(responses)
        return prompt_for_human_decision(
            DIRECTOR,
            input_fn=lambda _prompt: next(response_iterator),
            print_fn=printed.append,
            now_fn=lambda: HUMAN_TIMESTAMP,
        )

    def _directory_contents(self, directory: Path) -> dict[str, bytes]:
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
