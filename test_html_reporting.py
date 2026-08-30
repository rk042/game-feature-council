import re
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import test_evaluation
from council.cli import _preferred_report_path
from council.evaluation import create_comparison_record
from council.html_reporting import _cost_chart, render_html_report as render_council_html
from council.models import (
    ComparisonPreference,
    ConcernKind,
    EvidenceItem,
    EvidenceResolverRecord,
    EvidenceType,
    HumanAction,
    HumanComparisonReview,
    HumanDecision,
    ResolvedConcern,
    ResolutionStatus,
)
from council.reporting import (
    EXPECTED_ARTIFACT_FILES,
    EXPECTED_EVALUATION_ARTIFACT_FILES,
    EXPECTED_GENERALIST_ARTIFACT_FILES,
    render_html_report,
    render_markdown_report,
    update_comparison_with_human_review,
    update_run_with_human_decision,
    write_evaluation_artifacts,
    write_generalist_artifacts,
    write_run_artifacts,
)
from test_producer_agent import FEATURE


REVIEWED_AT = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


class HtmlReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluation = test_evaluation.EvaluationTests()
        self.context = self.evaluation._context()
        self.record = self.evaluation._run_record(self.context)

    def _cost_widths(self, costs: dict[str, Decimal]) -> list[Decimal]:
        html = _cost_chart(costs)
        raw_widths = re.findall(
            r'class="bar cost" style="width:([^%"]+)%"',
            html,
        )
        self.assertEqual(len(raw_widths), len(costs))
        for raw_width in raw_widths:
            self.assertRegex(raw_width, r"^\d+(?:\.\d+)?$")
        widths = [Decimal(raw_width) for raw_width in raw_widths]
        self.assertTrue(
            all(Decimal("0") <= width <= Decimal("100") for width in widths)
        )
        return widths

    def test_html_is_self_contained_grounded_and_has_disclaimer(self) -> None:
        before = self.record.model_dump(mode="json")
        html = render_html_report(self.record)

        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("<style>", html)
        self.assertIn("AI-Generated Analysis", html)
        self.assertIn("AI systems can make mistakes", html)
        self.assertNotRegex(html, r"(?i)https?://|@import|url\s*\(")
        self.assertNotRegex(
            html,
            r"(?i)<(?:script|link|img)[^>]+(?:src|href)\s*=",
        )
        self.assertIn(self.record.run_id, html)
        self.assertIn(self.record.ai_recommendation.value, html)
        self.assertIn(self.record.council_result.director.confidence.value, html)
        self.assertIn("Game Design", html)
        self.assertIn("Technical", html)
        self.assertIn("Analytics", html)
        self.assertIn("Scope / Risk", html)
        self.assertIn("Producer Synthesis", html)
        self.assertIn("Director recommendation", html)
        self.assertIn("repo-001", html)
        self.assertIn("100", html)
        self.assertIn("50", html)
        self.assertNotIn("PRIVATE SOURCE EXCERPT", html)
        self.assertEqual(render_html_report(self.record), html)
        self.assertEqual(self.record.model_dump(mode="json"), before)

    def test_council_html_starts_with_a_persisted_mvp_decision_brief(self) -> None:
        html = render_html_report(self.record)

        ordered_sections = (
            '<section id="what-you-asked"',
            '<section id="recommendation"',
            '<section id="what-we-have"',
            '<section id="what-is-open"',
            '<section id="recommended-mvp"',
            '<section id="not-building"',
            '<section id="what-we-learn"',
            '<section id="success-looks-like"',
            '<section id="effort-estimation"',
            '<section id="what-happens-next"',
            '<section id="detailed-analysis"',
        )
        positions = [html.index(section) for section in ordered_sections]
        self.assertEqual(positions, sorted(positions))
        divider = html.index('<section id="detailed-analysis"')
        self.assertIn("example-repository", html[:divider])
        self.assertIn("PROTOTYPE FIRST", html[:divider])
        self.assertIn("Enough to prototype", html[:divider])
        self.assertNotIn("Confidence: medium", html[:divider])
        self.assertIn("Confidence: medium", html[divider:])
        self.assertIn(
            self.record.council_result.director.experiment.smallest_experiment,
            html[:divider],
        )
        self.assertIn("Full production feature.", html[:divider])
        self.assertIn("Technical Audit Details", html[divider:])

    def test_brief_uses_only_consolidated_resolver_concerns(self) -> None:
        resolver = EvidenceResolverRecord(
            feature_sha256="a" * 64,
            source_concerns=[],
            concerns=[
                ResolvedConcern(
                    concern_id="concern-001",
                    kind=ConcernKind.UNKNOWN,
                    canonical_concern="Existing reward grant flow",
                    source_concern_ids=["source-001"],
                    status=ResolutionStatus.RESOLVED_FROM_REPOSITORY,
                    resolution="The supplied repository evidence identifies a reusable grant flow.",
                    evidence_ids=["repo-001"],
                ),
                ResolvedConcern(
                    concern_id="concern-002",
                    kind=ConcernKind.RISK,
                    canonical_concern="Eligibility remains unverified",
                    source_concern_ids=["source-002"],
                    status=ResolutionStatus.HUMAN_REPOSITORY_HELP,
                    why_unresolved="The selected evidence does not show eligibility enforcement.",
                    human_question="Which existing service enforces eligibility?",
                ),
            ],
            lookup_requests=[],
            supplemental_evidence=[],
            lookup_limitations=[],
            second_pass_occurred=False,
            attempted_calls=0,
            usage_complete=True,
            estimated_cost_usd=Decimal("0"),
            unpriced_models=[],
            pricing_snapshot_id=self.record.pricing_snapshot_id,
        )

        html = render_council_html(self.record, evidence_resolver=resolver)
        divider = html.index('<section id="detailed-analysis"')
        brief = html[:divider]

        self.assertIn("Existing reward grant flow", brief)
        self.assertIn("Eligibility remains unverified", brief)
        self.assertIn("The selected evidence does not show eligibility enforcement.", brief)
        self.assertIn("Evidence Resolution", html[divider:])
        self.assertNotIn("source-001", brief)

    def test_brief_never_turns_unavailable_effort_into_zero_days(self) -> None:
        record = self.record.model_copy(deep=True)
        record.council_result.director.effort.developer_days_min = None
        record.council_result.director.effort.developer_days_max = None
        record.council_result.director.effort.basis = "Selected evidence does not support a numeric estimate."

        html = render_html_report(record)
        brief = html[:html.index('<section id="detailed-analysis"')]

        self.assertIn("Not reliable yet", brief)
        self.assertIn("Selected evidence does not support a numeric estimate.", brief)
        self.assertNotIn("0-0 developer days", brief)

    def test_new_council_and_generalist_runs_write_both_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            output = root / "runs"
            output.mkdir()
            context = self.evaluation._context(str(target))
            record = self.evaluation._run_record(context)

            council_directory = write_run_artifacts(record, output)
            self.assertEqual(
                {path.name for path in council_directory.iterdir()},
                EXPECTED_ARTIFACT_FILES,
            )
            self.assertTrue((council_directory / "report.md").is_file())
            self.assertTrue((council_directory / "report.html").is_file())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            output = root / "runs"
            output.mkdir()
            context = self.evaluation._context(str(target))
            execution = self.evaluation._generalist_execution(context)
            directory = write_generalist_artifacts(
                "generalist-run",
                FEATURE,
                context,
                execution,
                output,
                started_at=REVIEWED_AT,
            )
            self.assertEqual(
                {path.name for path in directory.iterdir()},
                EXPECTED_GENERALIST_ARTIFACT_FILES,
            )
            html = (directory / "report.html").read_text(encoding="utf-8")
            self.assertIn("Generalist Decision", html)
            self.assertNotIn("Director Decision", html)
            self.assertNotIn("Council Decision", html)

    def test_all_persisted_strings_are_html_escaped(self) -> None:
        malicious = '<script src="https://evil.invalid/x.js">alert(1)</script>'
        result = self.record.council_result.model_copy(deep=True)
        result.context.branch = malicious
        result.context.evidence[0].file_path = malicious
        result.game_design.findings[0].statement = malicious
        result.game_design.evidence = [
            EvidenceItem(
                id="test-malicious",
                source_type=EvidenceType.AGENT_INFERENCE,
                claim=malicious,
                reason_it_matters=malicious,
            )
        ]
        record = self.record.model_copy(
            update={
                "feature_input": malicious,
                "council_result": result,
                "human_decision": HumanDecision(
                    action=HumanAction.ACCEPT,
                    final_decision=self.record.ai_recommendation,
                    note=malicious,
                    timestamp=REVIEWED_AT,
                ),
            }
        )

        html = render_html_report(record)

        self.assertNotIn("<script", html.casefold())
        self.assertIn("&lt;script", html)
        self.assertIn("&quot;https://evil.invalid/x.js&quot;", html)

    def test_pending_comparison_has_no_radar_or_default_scores(self) -> None:
        comparison = create_comparison_record(
            self.record,
            self.evaluation._generalist_execution(self.context),
        )

        html = render_html_report(self.record, comparison)

        self.assertIn("Human comparison pending", html)
        self.assertIn(
            f"council review --run {self.record.run_id}",
            html,
        )
        self.assertNotIn('id="comparison-radar"', html)
        self.assertNotIn("radar-series council-series", html)

    def test_missing_duration_is_omitted_without_fabricating_timeline(self) -> None:
        record = self.record.model_copy(deep=True)
        record.telemetry.roles["technical"].duration_ms = None

        html = render_html_report(record)

        self.assertNotIn("Technical: 0.000 ms", html)
        self.assertNotIn("Execution Timeline", html)
        self.assertIn("Execution Duration by Role", html)

    def test_radar_uses_only_completed_human_scores(self) -> None:
        comparison = create_comparison_record(
            self.record,
            self.evaluation._generalist_execution(self.context),
        )
        review = HumanComparisonReview(
            generalist_scores=self.evaluation._scores(2),
            council_scores=self.evaluation._scores(5),
            preference=ComparisonPreference.COUNCIL,
            reason="Council exposed more constraints.",
            timestamp=REVIEWED_AT,
        )
        comparison = comparison.model_copy(update={"human_review": review})

        html = render_html_report(self.record, comparison)

        self.assertIn('id="comparison-radar"', html)
        self.assertIn("Council exposed more constraints.", html)
        self.assertIn("<td>2</td><td>5</td>", html)
        self.assertIn("Human Evaluation", html)

    def test_unpriced_models_never_render_fake_zero_cost(self) -> None:
        record = self.record.model_copy(deep=True)
        record.estimated_cost_usd = None
        record.unpriced_models = ["unpriced-model"]
        for telemetry in record.telemetry.roles.values():
            telemetry.model = "unpriced-model"

        html = render_html_report(record)

        self.assertIn("Unpriced", html)
        self.assertNotIn("$0.000000", html)
        self.assertNotIn("Estimated Cost by Role", html)

    def test_cost_chart_normalizes_very_large_decimals_without_nan_or_inf(self) -> None:
        html = _cost_chart(
            {
                "game_design": Decimal("1e10000"),
                "technical": Decimal("5e9999"),
            }
        )

        widths = self._cost_widths(
            {
                "game_design": Decimal("1e10000"),
                "technical": Decimal("5e9999"),
            }
        )
        self.assertEqual(widths, [Decimal("100.000"), Decimal("50.000")])
        width_styles = re.findall(r'style="width:([^"]+)"', html)
        self.assertFalse(
            any(re.search(r"(?i)(?:nan|inf(?:inity)?)", value) for value in width_styles)
        )

    def test_cost_chart_handles_all_zero_costs(self) -> None:
        widths = self._cost_widths(
            {
                "game_design": Decimal("0"),
                "technical": Decimal("0"),
            }
        )

        self.assertEqual(widths, [Decimal("0.000"), Decimal("0.000")])

    def test_cost_chart_maps_all_equal_costs_to_equal_full_widths(self) -> None:
        widths = self._cost_widths(
            {
                "game_design": Decimal("1.25"),
                "technical": Decimal("1.25"),
                "analytics": Decimal("1.25"),
            }
        )

        self.assertEqual(widths, [Decimal("100.000")] * 3)

    def test_cost_chart_preserves_normal_mixed_proportions(self) -> None:
        widths = self._cost_widths(
            {
                "game_design": Decimal("0.25"),
                "technical": Decimal("1.00"),
            }
        )

        self.assertEqual(widths, [Decimal("25.000"), Decimal("100.000")])

    def test_council_only_does_not_fabricate_generalist_or_risk_scores(self) -> None:
        html = render_html_report(self.record)

        self.assertNotIn("Generalist Decision", html)
        self.assertNotIn("Generalist Confidence", html)
        self.assertNotIn('id="comparison-radar"', html)
        self.assertNotIn("Risk Matrix", html)
        self.assertIn("No probability or impact score is inferred", html)
        self.assertIn("Execution Duration by Role", html)
        self.assertNotIn("Execution Timeline", html)

    def test_markdown_and_html_agree_on_authoritative_values(self) -> None:
        markdown = render_markdown_report(self.record)
        html = render_html_report(self.record)
        values = (
            self.record.run_id,
            self.record.ai_recommendation.value,
            self.record.council_result.director.confidence.value,
            "repo-001",
            str(self.record.telemetry.total_usage.input_tokens),
            str(self.record.telemetry.total_usage.output_tokens),
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIn(value, markdown)
                self.assertIn(value, html)
        self.assertIn(str(self.record.estimated_cost_usd), markdown)
        self.assertIn(f"${self.record.estimated_cost_usd:.6f}", html)
        self.assertIn(
            self.record.council_result.game_design.findings[0].statement,
            html,
        )

    def test_human_review_regenerates_existing_html_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            output = root / "runs"
            output.mkdir()
            context = self.evaluation._context(str(target))
            record = self.evaluation._run_record(context)
            directory = write_run_artifacts(record, output)
            before = (directory / "report.html").read_text(encoding="utf-8")
            self.assertIn("Status: Pending", before)
            decision = HumanDecision(
                action=HumanAction.ACCEPT,
                final_decision=record.ai_recommendation,
                note="Approved after manual evidence review.",
                timestamp=REVIEWED_AT,
            )

            update_run_with_human_decision(directory, decision)
            first = (directory / "report.html").read_bytes()
            update_run_with_human_decision(directory, decision)
            second = (directory / "report.html").read_bytes()

            self.assertEqual(first, second)
            rendered = first.decode("utf-8")
            self.assertIn("Approved after manual evidence review.", rendered)
            self.assertIn("<dt>Action</dt><dd>accept</dd>", rendered)
            self.assertIn(
                "Director Questions - Resolved by Human Decision",
                rendered,
            )
            self.assertIn("Resolved: Choose a decision threshold.", rendered)

    def test_comparison_review_adds_radar_to_existing_html(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.mkdir()
            output = root / "runs"
            output.mkdir()
            context = self.evaluation._context(str(target))
            record = self.evaluation._run_record(context)
            directory = write_run_artifacts(record, output)
            comparison = create_comparison_record(
                record,
                self.evaluation._generalist_execution(context),
            )
            write_evaluation_artifacts(directory, comparison)
            pending = (directory / "report.html").read_text(encoding="utf-8")
            self.assertNotIn('id="comparison-radar"', pending)
            review = HumanComparisonReview(
                generalist_scores=self.evaluation._scores(3),
                council_scores=self.evaluation._scores(4),
                preference=ComparisonPreference.COUNCIL,
                reason="Council was more useful.",
                timestamp=REVIEWED_AT,
            )

            update_comparison_with_human_review(directory, review)
            completed = (directory / "report.html").read_text(encoding="utf-8")

            self.assertIn('id="comparison-radar"', completed)
            self.assertIn("Council was more useful.", completed)
            self.assertEqual(
                {path.name for path in directory.iterdir()},
                EXPECTED_EVALUATION_ARTIFACT_FILES,
            )

    def test_report_path_prefers_html_and_safely_falls_back_to_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            markdown = directory / "report.md"
            markdown.write_text("report", encoding="utf-8")
            self.assertEqual(_preferred_report_path(directory), markdown)
            html = directory / "report.html"
            html.write_text("<html></html>", encoding="utf-8")
            self.assertEqual(_preferred_report_path(directory), html)


if __name__ == "__main__":
    unittest.main()
