import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from council.agents import create_generalist_agent
from council.evaluation import (
    EvaluationError,
    build_context_identity,
    create_comparison_record,
    prompt_for_comparison_review,
    run_generalist,
)
from council.models import (
    ComparisonPreference,
    ComparisonRubricScores,
    ContextBuilderConfig,
    ContextBundle,
    CouncilExecution,
    CouncilResult,
    CouncilTelemetry,
    GeneralistExecution,
    HumanComparisonReview,
    RepositoryEvidence,
    RoleTelemetry,
    TokenUsage,
)
from council.orchestrator import AgentCallResult, render_specialist_input
from council.reporting import (
    EXPECTED_ARTIFACT_FILES,
    EXPECTED_EVALUATION_ARTIFACT_FILES,
    RunArtifactError,
    create_run_record,
    update_comparison_with_human_review,
    write_evaluation_artifacts,
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


REVIEWED_AT = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
ROLE_NAMES = (
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
    "producer",
    "director",
)


class FakeAgent:
    def __init__(self, model: str) -> None:
        self.model = model


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_generalist_prompt_gives_experiment_validity_precedence(self) -> None:
        prompt = Path("prompts/generalist.md").read_text(encoding="utf-8")

        self.assertIn(
            "Accidentally revoking an already-earned reward is first an "
            "experiment\nintegrity and implementation failure",
            prompt,
        )
        self.assertIn(
            "By itself, it belongs under\n`need_more_data`",
            prompt,
        )
        self.assertIn(
            "credible negative evidence from a valid experiment -> `kill`",
            prompt,
        )
        self.assertIn(
            "broken or uninterpretable execution -> `need_more_data`",
            prompt,
        )
        self.assertIn("invalid assignment", prompt)
        self.assertIn("unreliable persistence", prompt)
        self.assertIn("incorrect eligibility enforcement", prompt)
        self.assertIn("analytics or instrumentation failure", prompt)

    def test_generalist_factory_uses_authoritative_prompt_and_synthesis_model(
        self,
    ) -> None:
        prompt = Path("prompts/generalist.md").read_text(encoding="utf-8")
        with patch.dict(
            os.environ,
            {"COUNCIL_SYNTHESIS_MODEL": "synthetic-synthesis-model"},
        ):
            agent = create_generalist_agent()

        self.assertEqual(agent.name, "Generalist Baseline")
        self.assertEqual(agent.model, "synthetic-synthesis-model")
        self.assertEqual(agent.output_type, type(DIRECTOR))
        self.assertEqual(agent.instructions, prompt)

    async def test_generalist_isolated_one_call_with_telemetry_and_pricing(
        self,
    ) -> None:
        context = self._context()
        captured_inputs: list[str] = []

        async def execute(_agent: FakeAgent, input_text: str):
            captured_inputs.append(input_text)
            return AgentCallResult(
                output=DIRECTOR,
                usage=TokenUsage(
                    requests=1,
                    input_tokens=1_000,
                    output_tokens=200,
                    total_tokens=1_200,
                ),
            )

        with patch(
            "council.evaluation.create_generalist_agent",
            return_value=FakeAgent("gpt-5.4-nano"),
        ):
            execution = await run_generalist(FEATURE, context, execute)

        self.assertEqual(len(captured_inputs), 1)
        self.assertEqual(
            captured_inputs[0],
            render_specialist_input(FEATURE, context),
        )
        self.assertNotIn(GAME_DESIGN.model_dump_json(indent=2), captured_inputs[0])
        self.assertNotIn(PRODUCER.model_dump_json(indent=2), captured_inputs[0])
        self.assertNotIn(DIRECTOR.model_dump_json(indent=2), captured_inputs[0])
        self.assertEqual(execution.result, DIRECTOR)
        self.assertEqual(execution.telemetry.model, "gpt-5.4-nano")
        self.assertGreaterEqual(execution.telemetry.duration_ms, 0)
        self.assertEqual(execution.telemetry.usage.input_tokens, 1_000)
        self.assertEqual(execution.telemetry.usage.output_tokens, 200)
        self.assertEqual(execution.telemetry.usage.total_tokens, 1_200)
        self.assertEqual(execution.estimated_cost_usd, Decimal("0.000450"))
        self.assertEqual(execution.unpriced_models, [])

        with patch(
            "council.evaluation.create_generalist_agent",
            return_value=FakeAgent("unpriced-model"),
        ):
            unpriced = await run_generalist(FEATURE, context, execute)
        self.assertIsNone(unpriced.estimated_cost_usd)
        self.assertEqual(unpriced.unpriced_models, ["unpriced-model"])

    async def test_generalist_failure_surfaces_without_comparison(self) -> None:
        calls = 0

        async def execute(_agent: FakeAgent, _input_text: str):
            nonlocal calls
            calls += 1
            raise RuntimeError("synthetic baseline failure")

        with patch(
            "council.evaluation.create_generalist_agent",
            return_value=FakeAgent("gpt-5.4-nano"),
        ):
            with self.assertRaisesRegex(
                EvaluationError,
                "Generalist execution failed.*synthetic baseline failure",
            ):
                await run_generalist(FEATURE, self._context(), execute)

        self.assertEqual(calls, 1)

    def test_comparison_enforces_same_feature_and_context(self) -> None:
        context = self._context()
        council_run = self._run_record(context)
        generalist = self._generalist_execution(context)

        comparison = create_comparison_record(council_run, generalist)

        self.assertEqual(comparison.context_identity.repository_path, context.repository_path)
        self.assertEqual(comparison.context_identity.commit_sha, context.commit_sha)
        self.assertEqual(
            comparison.context_identity.working_tree_dirty,
            context.working_tree_dirty,
        )
        self.assertEqual(
            [item.id for item in comparison.context_identity.evidence_manifest],
            ["repo-001"],
        )
        self.assertEqual(
            comparison.context_identity,
            build_context_identity(context),
        )
        self.assertEqual(comparison.council_metrics.total_tokens, 900)
        self.assertEqual(comparison.generalist_metrics.total_tokens, 1_200)
        self.assertEqual(comparison.generalist_metrics.duration_ms, 75.0)
        self.assertEqual(
            comparison.generalist_metrics.estimated_cost_usd,
            Decimal("0.000450"),
        )
        self.assertFalse(hasattr(comparison, "winner"))
        self.assertIsNone(comparison.human_review)

        wrong_feature = generalist.model_copy(update={"feature_sha256": "0" * 64})
        with self.assertRaisesRegex(EvaluationError, "feature input"):
            create_comparison_record(council_run, wrong_feature)

        changed_context = context.model_copy(deep=True)
        changed_context.evidence[0].text = "Different evidence content."
        wrong_context = generalist.model_copy(
            update={"context_identity": build_context_identity(changed_context)}
        )
        with self.assertRaisesRegex(EvaluationError, "ContextBundle"):
            create_comparison_record(council_run, wrong_context)

    def test_rubric_bounds_and_preferences_are_typed(self) -> None:
        scores = self._scores(1)
        self.assertEqual(scores.grounding, 1)
        self.assertEqual(self._scores(5).conciseness, 5)

        with self.assertRaises(ValidationError):
            self._scores(0)
        with self.assertRaises(ValidationError):
            self._scores(6)

        self.assertEqual(ComparisonPreference("generalist").value, "generalist")
        self.assertEqual(ComparisonPreference("council").value, "council")
        self.assertEqual(ComparisonPreference("tie").value, "tie")

    def test_human_comparison_prompt_validates_scores_and_preference(self) -> None:
        responses = iter(
            [
                "0",
                "5",
                *(["4"] * 7),
                "6",
                "3",
                *(["3"] * 7),
                "invalid",
                "b",
                "",
                "Council surfaced more useful constraints.",
            ]
        )
        printed: list[str] = []

        review = prompt_for_comparison_review(
            input_fn=lambda _prompt: next(responses),
            print_fn=printed.append,
            now_fn=lambda: REVIEWED_AT,
        )

        self.assertEqual(review.generalist_scores.grounding, 5)
        self.assertEqual(review.council_scores.grounding, 3)
        self.assertEqual(review.preference, ComparisonPreference.COUNCIL)
        self.assertEqual(
            review.reason,
            "Council surfaced more useful constraints.",
        )
        self.assertEqual(review.timestamp, REVIEWED_AT)
        self.assertTrue(any("integer from 1 to 5" in line for line in printed))
        self.assertTrue(any("Enter A, B, or T" in line for line in printed))
        self.assertTrue(any("reason is required" in line for line in printed))

    def test_evaluation_artifact_lifecycle_preserves_council_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target_repository = root / "target-repository"
            target_repository.mkdir()
            output_root = root / "runs"
            output_root.mkdir()
            context = self._context(str(target_repository))
            council_run = self._run_record(context)
            run_directory = write_run_artifacts(council_run, output_root)
            initial_report = (run_directory / "report.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("## Generalist Baseline Comparison", initial_report)
            council_artifacts_before = {
                name: (run_directory / name).read_bytes()
                for name in EXPECTED_ARTIFACT_FILES
                if name not in {"report.md", "report.html"}
            }
            comparison = create_comparison_record(
                council_run,
                self._generalist_execution(context),
            )

            write_evaluation_artifacts(run_directory, comparison)

            self.assertEqual(
                {path.name for path in run_directory.iterdir()},
                EXPECTED_EVALUATION_ARTIFACT_FILES,
            )
            generalist_payload = json.loads(
                (run_directory / "generalist.json").read_text(encoding="utf-8")
            )
            comparison_payload = json.loads(
                (run_directory / "comparison.json").read_text(encoding="utf-8")
            )
            self.assertEqual(generalist_payload["result"]["decision"], "PROTOTYPE_FIRST")
            self.assertIsNone(comparison_payload["human_review"])
            council_artifacts_after = {
                name: (run_directory / name).read_bytes()
                for name in council_artifacts_before
            }
            self.assertEqual(council_artifacts_after, council_artifacts_before)
            report = (run_directory / "report.md").read_text(encoding="utf-8")
            self.assertIn("## Generalist Baseline Comparison", report)
            self.assertIn("Human comparison review: Pending", report)
            self.assertNotIn("PRIVATE SOURCE EXCERPT", report)

            review = HumanComparisonReview(
                generalist_scores=self._scores(3),
                council_scores=self._scores(4),
                preference=ComparisonPreference.COUNCIL,
                reason="Council provided more actionable planning support.",
                insights=[],
                timestamp=REVIEWED_AT,
            )
            updated = update_comparison_with_human_review(
                run_directory,
                review,
            )
            self.assertEqual(updated.human_review, review)
            updated_report = (run_directory / "report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Human preference: **council**", updated_report)
            self.assertIn("### Human Rubric", updated_report)

            with self.assertRaisesRegex(
                RunArtifactError,
                "already exist",
            ):
                write_evaluation_artifacts(run_directory, comparison)

    def _context(
        self,
        repository_path: str = "/synthetic/example-repository",
    ) -> ContextBundle:
        evidence = RepositoryEvidence(
            id="repo-001",
            file_path="src/ExampleFeatureService.py",
            selection_reasons=["feature term match", "source relevance"],
            matched_terms=["repeat", "engagement"],
            text="PRIVATE SOURCE EXCERPT",
            truncated=False,
        )
        return ContextBundle(
            repository_path=repository_path,
            commit_sha="3" * 40,
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
                started_at=REVIEWED_AT,
                total_duration_ms=180.0,
                roles=roles,
                total_usage=TokenUsage(
                    requests=6,
                    input_tokens=600,
                    output_tokens=300,
                    total_tokens=900,
                ),
            ),
        )

    def _run_record(self, context: ContextBundle):
        return create_run_record(
            FEATURE,
            self._council_execution(context),
            run_id="run-001",
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
                duration_ms=75.0,
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


if __name__ == "__main__":
    unittest.main()
