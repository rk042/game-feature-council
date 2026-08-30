import hashlib
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_cli
from council.cli import (
    PlainOutput,
    PreflightEstimate,
    RunArguments,
    _enforce_cost_cap,
    _print_completion,
    _run_command,
    _run_feature_refinement_workflow,
)
from council.refinement import create_feature_refinement_record
from council.models import (
    FeatureRefinementExecution,
    FeatureRefinerResult,
    RoleTelemetry,
    TokenUsage,
)


MODEL_ENVIRONMENT = {
    "COUNCIL_SPECIALIST_MODEL": "gpt-5.4-nano",
    "COUNCIL_SYNTHESIS_MODEL": "gpt-5.4-nano",
    "COUNCIL_REFINER_MODEL": "gpt-5.4-nano",
    "OPENAI_API_KEY": "phase4a-test-secret",
}
ORIGINAL = "noisy original feature"


def _result(number: int) -> FeatureRefinerResult:
    return FeatureRefinerResult(
        concise_interpretation=[f"Interpretation {number}"],
        refined_brief=f"Approved canonical brief {number}",
        unresolved_points=[f"Unknown {number}"],
        preserved_constraints=[f"Constraint {number}"],
    )


def _execution(
    number: int,
    *,
    cost: Decimal | None = Decimal("0.001"),
) -> FeatureRefinementExecution:
    return FeatureRefinementExecution(
        result=_result(number),
        telemetry=RoleTelemetry(
            model="gpt-5.4-nano",
            duration_ms=100,
            usage=TokenUsage(
                requests=1,
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
            ),
        ),
        estimated_cost_usd=cost,
        unpriced_models=[] if cost is not None else ["unpriced-model"],
        pricing_snapshot_id="openai-2026-08-28",
    )


class FeatureRefinerCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_no_skips_without_refiner_call(self) -> None:
        output: list[str] = []
        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch(
                "council.refinement.run_feature_refiner",
                new_callable=AsyncMock,
            ) as run_refiner,
        ):
            selection = await _run_feature_refinement_workflow(
                ORIGINAL,
                None,
                input_fn=lambda _prompt: "",
                output=PlainOutput(output.append),
            )

        self.assertIsNotNone(selection)
        self.assertEqual(selection.feature, ORIGINAL)
        self.assertIsNone(selection.refinement)
        run_refiner.assert_not_awaited()
        self.assertIn("feature description only", "\n".join(output))

    async def test_approved_result_is_shown_and_selected_exactly(self) -> None:
        output: list[str] = []
        answers = iter(["yes", ""])
        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch(
                "council.refinement.run_feature_refiner",
                AsyncMock(return_value=_execution(1)),
            ) as run_refiner,
        ):
            selection = await _run_feature_refinement_workflow(
                ORIGINAL,
                None,
                input_fn=lambda _prompt: next(answers),
                output=PlainOutput(output.append),
            )

        self.assertEqual(selection.feature, _result(1).refined_brief)
        self.assertTrue(selection.refinement.approved)
        self.assertEqual(selection.prior_api_calls, 1)
        run_refiner.assert_awaited_once_with(
            ORIGINAL,
            previous_result=None,
            user_correction=None,
            model="gpt-5.4-nano",
        )
        rendered = "\n".join(output)
        self.assertIn(_result(1).refined_brief, rendered)
        self.assertIn("Constraint 1", rendered)

    async def test_two_corrections_use_history_and_three_call_cap(self) -> None:
        answers = iter(
            [
                "yes",
                "n",
                "first correction",
                "yes",
                "n",
                "second correction",
                "yes",
                "n",
                "1",
            ]
        )
        run_refiner = AsyncMock(
            side_effect=[_execution(1), _execution(2), _execution(3)]
        )
        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch(
                "council.refinement.run_feature_refiner",
                run_refiner,
            ),
        ):
            selection = await _run_feature_refinement_workflow(
                ORIGINAL,
                None,
                input_fn=lambda _prompt: next(answers),
                output=PlainOutput(lambda _text: None),
            )

        self.assertEqual(run_refiner.await_count, 3)
        self.assertEqual(
            run_refiner.await_args_list[1].kwargs,
            {
                "previous_result": _result(1),
                "user_correction": "first correction",
                "model": "gpt-5.4-nano",
            },
        )
        self.assertEqual(
            run_refiner.await_args_list[2].kwargs,
            {
                "previous_result": _result(2),
                "user_correction": "second correction",
                "model": "gpt-5.4-nano",
            },
        )
        self.assertEqual(selection.feature, _result(3).refined_brief)
        self.assertEqual(selection.refinement.refinement_rounds, 3)

    async def test_correction_costs_accumulate_and_block_next_call(self) -> None:
        estimate = PreflightEstimate(
            expected_calls=1,
            rough_input_tokens=100,
            output_token_allowance=100,
            estimated_cost_low_usd=Decimal("0.002"),
            estimated_cost_high_usd=Decimal("0.003"),
            conservative_max_cost_usd=Decimal("0.006"),
            unpriced_models=(),
        )
        answers = iter(["yes", "n", "correct it", "yes", "2"])
        run_refiner = AsyncMock(
            return_value=_execution(1, cost=Decimal("0.006"))
        )
        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch(
                "council.cli.build_refiner_preflight_estimate",
                return_value=estimate,
            ),
            patch(
                "council.refinement.run_feature_refiner",
                run_refiner,
            ),
        ):
            selection = await _run_feature_refinement_workflow(
                ORIGINAL,
                Decimal("0.010"),
                input_fn=lambda _prompt: next(answers),
                output=PlainOutput(lambda _text: None),
            )

        self.assertEqual(run_refiner.await_count, 1)
        self.assertEqual(selection.feature, ORIGINAL)
        self.assertFalse(selection.refinement.approved)

    async def test_unpriced_refiner_with_cap_fails_before_call(self) -> None:
        estimate = PreflightEstimate(
            expected_calls=1,
            rough_input_tokens=100,
            output_token_allowance=100,
            estimated_cost_low_usd=None,
            estimated_cost_high_usd=None,
            conservative_max_cost_usd=None,
            unpriced_models=("unpriced-model",),
        )
        run_refiner = AsyncMock()
        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch(
                "council.cli.build_refiner_preflight_estimate",
                return_value=estimate,
            ),
            patch(
                "council.refinement.run_feature_refiner",
                run_refiner,
            ),
        ):
            selection = await _run_feature_refinement_workflow(
                ORIGINAL,
                Decimal("0.10"),
                input_fn=lambda _prompt: "yes",
                output=PlainOutput(lambda _text: None),
            )

        self.assertEqual(selection.feature, ORIGINAL)
        run_refiner.assert_not_awaited()

    async def test_failure_is_redacted_and_never_retried(self) -> None:
        output: list[str] = []
        answers = iter(["yes", "1"])
        secret = MODEL_ENVIRONMENT["OPENAI_API_KEY"]
        run_refiner = AsyncMock(
            side_effect=RuntimeError(f"Authorization: Bearer {secret}")
        )
        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch(
                "council.refinement.run_feature_refiner",
                run_refiner,
            ),
        ):
            selection = await _run_feature_refinement_workflow(
                ORIGINAL,
                None,
                input_fn=lambda _prompt: next(answers),
                output=PlainOutput(output.append),
            )

        rendered = "\n".join(output)
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertTrue(selection.usage_unavailable)
        self.assertEqual(selection.prior_api_calls, 1)
        self.assertIsNotNone(selection.refinement)
        self.assertFalse(selection.refinement.approved)
        self.assertFalse(selection.refinement.usage_complete)
        self.assertEqual(selection.refinement.attempted_calls, 1)
        self.assertEqual(selection.refinement.refinement_rounds, 0)
        self.assertIn(
            "did not return usage telemetry",
            selection.refinement.usage_unavailable_reason,
        )
        run_refiner.assert_awaited_once()

    async def test_successful_round_then_failed_round_preserves_known_usage(
        self,
    ) -> None:
        answers = iter(["yes", "n", "correct the colour", "yes", "1"])
        run_refiner = AsyncMock(
            side_effect=[_execution(1, cost=Decimal("0.004")), RuntimeError("failed")]
        )
        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch("council.refinement.run_feature_refiner", run_refiner),
        ):
            selection = await _run_feature_refinement_workflow(
                ORIGINAL,
                None,
                input_fn=lambda _prompt: next(answers),
                output=PlainOutput(lambda _text: None),
            )

        self.assertEqual(run_refiner.await_count, 2)
        self.assertFalse(selection.refinement.usage_complete)
        self.assertEqual(selection.refinement.attempted_calls, 2)
        self.assertEqual(selection.refinement.refinement_rounds, 1)
        self.assertEqual(selection.refinement.estimated_cost_usd, Decimal("0.004"))

    def test_downstream_cap_includes_completed_refiner_cost(self) -> None:
        downstream = PreflightEstimate(
            expected_calls=7,
            rough_input_tokens=100,
            output_token_allowance=100,
            estimated_cost_low_usd=Decimal("0.02"),
            estimated_cost_high_usd=Decimal("0.04"),
            conservative_max_cost_usd=Decimal("0.096"),
            unpriced_models=(),
        )
        _enforce_cost_cap(
            downstream,
            Decimal("0.100"),
            completed_cost=Decimal("0.004"),
        )
        with self.assertRaisesRegex(Exception, "Cumulative conservative"):
            _enforce_cost_cap(
                downstream,
                Decimal("0.099"),
                completed_cost=Decimal("0.004"),
            )
        with self.assertRaisesRegex(Exception, "cannot be enforced"):
            _enforce_cost_cap(
                downstream,
                Decimal("0.100"),
                completed_cost=Decimal("0"),
                prior_usage_unavailable=True,
            )

    def test_incomplete_usage_summary_shows_known_cost_not_a_false_total(self) -> None:
        helper = test_cli.CliTests()
        context = helper._context("D:/synthetic-target")
        refinement = create_feature_refinement_record(
            ORIGINAL,
            [],
            approved=False,
            attempted_calls=1,
            usage_complete=False,
            usage_unavailable_reason=(
                "One Feature Refiner attempt did not return usage telemetry."
            ),
            model="gpt-5.4-nano",
        )
        output: list[str] = []

        _print_completion(
            "council",
            "run-id",
            Path("D:/runs/run-id"),
            Path("D:/runs/run-id/report.html"),
            helper._council_execution(context),
            None,
            1.0,
            PlainOutput(output.append),
            feature_refinement=refinement,
        )

        rendered = "\n".join(output)
        self.assertIn("Known estimated cost USD:", rendered)
        self.assertIn("Overall session cost: incomplete", rendered)
        self.assertIn("did not return usage telemetry", rendered)
        self.assertNotIn("Actual estimated cost USD:", rendered)

    async def test_approved_feature_and_same_context_reach_both_paths(self) -> None:
        refined = _result(1).refined_brief
        helper = test_cli.CliTests()
        context = helper._context("D:/synthetic-target").model_copy(
            update={"feature_input": refined}
        )
        council = helper._council_execution(context)
        generalist = helper._generalist_execution(context).model_copy(
            update={
                "feature_sha256": hashlib.sha256(
                    refined.encode("utf-8")
                ).hexdigest()
            }
        )
        record = (await self._approved_selection()).refinement
        seen: dict[str, object] = {}

        def build_context(repo: str, feature: str):
            seen["context_feature"] = feature
            return context

        async def run_council(feature: str, supplied_context: object, **_kwargs):
            seen["council"] = (feature, supplied_context)
            return council

        async def run_generalist(feature: str, supplied_context: object, **_kwargs):
            seen["generalist"] = (feature, supplied_context)
            return generalist

        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.build_context", side_effect=build_context),
                patch(
                    "council.cli.run_council_with_telemetry",
                    side_effect=run_council,
                ),
                patch("council.cli.run_generalist", side_effect=run_generalist),
                patch(
                    "council.cli._write_mode_artifacts",
                    return_value=(
                        "run-id",
                        Path(temporary_directory) / "run-id",
                        Path(temporary_directory) / "run-id" / "report.html",
                    ),
                ) as write_artifacts,
            ):
                code = await _run_command(
                    RunArguments(
                        repo="D:/synthetic-target",
                        feature=refined,
                        feature_file=None,
                        mode="both",
                        max_cost_usd=None,
                        dry_run=False,
                        yes=True,
                        output_dir=temporary_directory,
                        feature_refinement=record,
                        prior_api_calls=1,
                    ),
                    input_fn=lambda _prompt: "",
                    output=PlainOutput(lambda _text: None),
                )

        self.assertEqual(code, 0)
        self.assertEqual(seen["context_feature"], refined)
        self.assertEqual(seen["council"][0], refined)
        self.assertEqual(seen["generalist"][0], refined)
        self.assertIs(seen["council"][1], context)
        self.assertIs(seen["generalist"][1], context)
        self.assertIs(
            write_artifacts.call_args.kwargs["feature_refinement"],
            record,
        )

    async def _approved_selection(self):
        answers = iter(["yes", ""])
        with (
            patch.dict(os.environ, MODEL_ENVIRONMENT, clear=False),
            patch(
                "council.refinement.run_feature_refiner",
                AsyncMock(return_value=_execution(1)),
            ),
        ):
            return await _run_feature_refinement_workflow(
                ORIGINAL,
                None,
                input_fn=lambda _prompt: next(answers),
                output=PlainOutput(lambda _text: None),
            )


if __name__ == "__main__":
    unittest.main()
