import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import test_evaluation
from council.evaluation import build_context_identity, create_comparison_record
from council.models import HumanAction, HumanDecision
from council.refinement import create_feature_refinement_record, refinement_round
from council.reporting import (
    RunArtifactError,
    load_review_artifacts,
    render_html_report,
    render_markdown_report,
    update_run_with_human_decision,
    write_evaluation_artifacts,
    write_generalist_artifacts,
    write_run_artifacts,
)
from test_phase4a_cli import ORIGINAL, _execution


class FeatureRefinementReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        helper = test_evaluation.EvaluationTests()
        self.brief = _execution(1).result.refined_brief
        self.context = helper._context().model_copy(
            update={"feature_input": self.brief}
        )
        base = helper._run_record(helper._context())
        council_result = base.council_result.model_copy(
            update={"context": self.context}
        )
        self.record = base.model_copy(
            update={
                "feature_input": self.brief,
                "council_result": council_result,
            }
        )
        self.refinement = create_feature_refinement_record(
            ORIGINAL,
            [refinement_round(_execution(1), user_correction=None)],
            approved=True,
        )

    def test_optional_artifact_preserves_original_and_canonical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = write_run_artifacts(
                self.record,
                temporary_directory,
                feature_refinement=self.refinement,
            )

            payload = json.loads(
                (directory / "feature_refinement.json").read_text(
                    encoding="utf-8"
                )
            )
            input_payload = json.loads(
                (directory / "input.json").read_text(encoding="utf-8")
            )
            artifacts = load_review_artifacts(
                temporary_directory,
                self.record.run_id,
            )

        self.assertEqual(payload["original_feature"], ORIGINAL)
        self.assertEqual(payload["approved_refined_feature"], self.brief)
        self.assertEqual(input_payload["feature_input"], self.brief)
        self.assertEqual(artifacts.feature_refinement, self.refinement)

    def test_skipped_refinement_creates_no_artifact_or_report_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = write_run_artifacts(self.record, temporary_directory)
            markdown = (directory / "report.md").read_text(encoding="utf-8")
            html = (directory / "report.html").read_text(encoding="utf-8")

            self.assertFalse((directory / "feature_refinement.json").exists())
            self.assertNotIn("Feature Refinement", markdown)
            self.assertNotIn('id="feature-refinement"', html)

    def test_markdown_and_html_distinguish_original_from_approved_and_escape(self) -> None:
        malicious = self.refinement.model_copy(
            update={"original_feature": '<script>alert("x")</script>'}
        )
        markdown = render_markdown_report(
            self.record,
            feature_refinement=malicious,
        )
        html = render_html_report(
            self.record,
            feature_refinement=malicious,
        )

        self.assertIn("## Original Feature Request", markdown)
        self.assertIn("## Approved AI Interpretation", markdown)
        self.assertIn(self.brief, markdown)
        self.assertIn("Unresolved Before Repository Analysis", markdown)
        self.assertIn("Feature Refiner / Session", markdown)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertIn("Approved Refined Feature Brief", html)
        self.assertIn("Shared preprocessing telemetry", html)
        self.assertEqual(
            render_html_report(
                self.record,
                feature_refinement=malicious,
            ),
            html,
        )

    def test_unapproved_record_labels_candidate_as_not_evaluated(self) -> None:
        refinement = create_feature_refinement_record(
            self.brief,
            [refinement_round(_execution(1), user_correction=None)],
            approved=False,
        )
        original_record = self.record.model_copy(
            update={"feature_input": self.brief}
        )
        markdown = render_markdown_report(
            original_record,
            feature_refinement=refinement,
        )
        html = render_html_report(
            original_record,
            feature_refinement=refinement,
        )
        self.assertIn("original request was evaluated", markdown)
        self.assertIn("not evaluated", html)

    def test_empty_unresolved_points_do_not_create_a_placeholder_section(self) -> None:
        execution = _execution(1)
        execution = execution.model_copy(
            update={
                "result": execution.result.model_copy(
                    update={"unresolved_points": []}
                )
            }
        )
        refinement = create_feature_refinement_record(
            ORIGINAL,
            [refinement_round(execution, user_correction=None)],
            approved=True,
        )
        markdown = render_markdown_report(
            self.record,
            feature_refinement=refinement,
        )
        html = render_html_report(
            self.record,
            feature_refinement=refinement,
        )
        self.assertNotIn("Unresolved Before Repository Analysis", markdown)
        self.assertNotIn("Unresolved Before Repository Analysis", html)

    def test_mismatched_refinement_fails_before_directory_creation(self) -> None:
        mismatch = self.refinement.model_copy(
            update={"approved_refined_feature": "different brief"}
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(
                RunArtifactError,
                "canonical evaluated feature",
            ):
                write_run_artifacts(
                    self.record,
                    temporary_directory,
                    feature_refinement=mismatch,
                )
            self.assertEqual(list(Path(temporary_directory).iterdir()), [])

    def test_offline_review_preserves_refinement_bytes_and_report_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = write_run_artifacts(
                self.record,
                temporary_directory,
                feature_refinement=self.refinement,
            )
            before = (directory / "feature_refinement.json").read_bytes()
            update_run_with_human_decision(
                directory,
                HumanDecision(
                    action=HumanAction.ACCEPT,
                    final_decision=self.record.ai_recommendation,
                    note="Approved offline.",
                    timestamp=datetime(2026, 8, 30, tzinfo=timezone.utc),
                ),
            )
            after = (directory / "feature_refinement.json").read_bytes()
            markdown = (directory / "report.md").read_text(encoding="utf-8")
            html = (directory / "report.html").read_text(encoding="utf-8")

        self.assertEqual(before, after)
        self.assertIn("Approved AI Interpretation", markdown)
        self.assertIn('id="feature-refinement"', html)

    def test_combined_report_regeneration_retains_refinement_provenance(self) -> None:
        helper = test_evaluation.EvaluationTests()
        generalist = helper._generalist_execution(self.context).model_copy(
            update={
                "feature_sha256": hashlib.sha256(
                    self.brief.encode("utf-8")
                ).hexdigest(),
                "context_identity": build_context_identity(self.context),
            }
        )
        comparison = create_comparison_record(self.record, generalist)
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = write_run_artifacts(
                self.record,
                temporary_directory,
                feature_refinement=self.refinement,
            )
            before = (directory / "feature_refinement.json").read_bytes()
            write_evaluation_artifacts(directory, comparison)
            after = (directory / "feature_refinement.json").read_bytes()
            markdown = (directory / "report.md").read_text(encoding="utf-8")
            html = (directory / "report.html").read_text(encoding="utf-8")

        self.assertEqual(before, after)
        self.assertIn("Approved AI Interpretation", markdown)
        self.assertIn('id="feature-refinement"', html)
        self.assertIn("Generalist Comparison", html)

    def test_generalist_only_refinement_does_not_fabricate_council_fields(self) -> None:
        helper = test_evaluation.EvaluationTests()
        generalist = helper._generalist_execution(helper._context()).model_copy(
            update={
                "feature_sha256": hashlib.sha256(
                    self.brief.encode("utf-8")
                ).hexdigest(),
                "context_identity": build_context_identity(self.context),
            }
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = write_generalist_artifacts(
                "generalist-refined",
                self.brief,
                self.context,
                generalist,
                temporary_directory,
                started_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
                feature_refinement=self.refinement,
            )
            markdown = (directory / "report.md").read_text(encoding="utf-8")
            html = (directory / "report.html").read_text(encoding="utf-8")

        self.assertIn("# Generalist Baseline Report", markdown)
        self.assertIn("Approved AI Interpretation", markdown)
        self.assertNotIn("Director Recommendation", markdown)
        self.assertNotIn("Producer Synthesis", html)

    def test_incomplete_failed_first_attempt_persists_and_marks_cost_incomplete(
        self,
    ) -> None:
        helper = test_evaluation.EvaluationTests()
        context = helper._context()
        record = helper._run_record(context)
        refinement = create_feature_refinement_record(
            record.feature_input,
            [],
            approved=False,
            attempted_calls=1,
            usage_complete=False,
            usage_unavailable_reason=(
                "One Feature Refiner attempt did not return usage telemetry."
            ),
            model="gpt-5.4-nano",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = write_run_artifacts(
                record,
                temporary_directory,
                feature_refinement=refinement,
            )
            persisted = json.loads(
                (directory / "feature_refinement.json").read_text(
                    encoding="utf-8"
                )
            )
            markdown = (directory / "report.md").read_text(encoding="utf-8")
            html = (directory / "report.html").read_text(encoding="utf-8")
            update_run_with_human_decision(
                directory,
                HumanDecision(
                    action=HumanAction.ACCEPT,
                    final_decision=record.ai_recommendation,
                    timestamp=datetime(2026, 8, 30, tzinfo=timezone.utc),
                ),
            )
            regenerated = (directory / "report.md").read_text(encoding="utf-8")

        self.assertFalse(persisted["usage_complete"])
        self.assertEqual(persisted["attempted_calls"], 1)
        self.assertEqual(persisted["refinement_rounds"], 0)
        self.assertIsNone(persisted["estimated_cost_usd"])
        self.assertIn("Feature Refiner usage: Incomplete", markdown)
        self.assertIn("Overall session cost: Incomplete / unavailable", markdown)
        self.assertIn("Session Cost", html)
        self.assertIn("Incomplete", html)
        self.assertNotIn("Overall estimated cost USD:", markdown)
        self.assertIn("Overall session cost: Incomplete / unavailable", regenerated)

    def test_failed_later_round_keeps_known_cost_but_not_a_complete_total(self) -> None:
        helper = test_evaluation.EvaluationTests()
        context = helper._context()
        record = helper._run_record(context)
        refinement = create_feature_refinement_record(
            ORIGINAL,
            [refinement_round(_execution(1), user_correction=None)],
            approved=False,
            attempted_calls=2,
            usage_complete=False,
            usage_unavailable_reason=(
                "One Feature Refiner attempt did not return usage telemetry."
            ),
        )
        markdown = render_markdown_report(record, feature_refinement=refinement)
        html = render_html_report(record, feature_refinement=refinement)

        self.assertIn("Known Refiner cost USD: 0.001", markdown)
        self.assertIn("Known session cost USD:", markdown)
        self.assertIn("Overall session cost: Incomplete / unavailable", markdown)
        self.assertNotIn("Overall estimated cost USD:", markdown)
        self.assertIn("Known Session Cost", html)
        self.assertIn("Session Cost", html)


if __name__ == "__main__":
    unittest.main()
