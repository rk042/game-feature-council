import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from council.execution import AgentCallResult
from council.models import (
    FeatureRefinementExecution,
    FeatureRefinementRecord,
    FeatureRefinerResult,
    RoleTelemetry,
    TokenUsage,
)
from council.refinement import (
    FeatureRefinementError,
    configured_refiner_model,
    create_feature_refinement_record,
    refinement_round,
    render_feature_refiner_input,
    run_feature_refiner,
)


RESULT = FeatureRefinerResult(
    concise_interpretation=["Create a special gem from a five-gem merge."],
    refined_brief="Feature Goal\nCreate a special gem after a five-gem merge.",
    unresolved_points=["The exact five-gem pattern is not specified."],
    preserved_constraints=["Gameplay continues after activation."],
)


class FeatureRefinementTests(unittest.IsolatedAsyncioTestCase):
    def test_model_override_fallback_and_missing_configuration(self) -> None:
        self.assertEqual(
            configured_refiner_model(
                {
                    "COUNCIL_REFINER_MODEL": "refiner-model",
                    "COUNCIL_SYNTHESIS_MODEL": "synthesis-model",
                }
            ),
            "refiner-model",
        )
        self.assertEqual(
            configured_refiner_model(
                {"COUNCIL_SYNTHESIS_MODEL": "synthesis-model"}
            ),
            "synthesis-model",
        )
        with self.assertRaisesRegex(
            FeatureRefinementError,
            "COUNCIL_REFINER_MODEL or COUNCIL_SYNTHESIS_MODEL",
        ):
            configured_refiner_model({})

    def test_inputs_are_deterministic_bounded_and_correction_preserves_history(
        self,
    ) -> None:
        original = "生の feature text"
        initial = render_feature_refiner_input(original)
        self.assertEqual(initial, render_feature_refiner_input(original))
        payload = json.loads(initial.split("\n", 1)[1])
        self.assertEqual(payload, {"original_feature_request": original})
        self.assertNotIn("repository", initial.casefold())

        correction = render_feature_refiner_input(
            original,
            previous_result=RESULT,
            user_correction="Use the normal gem's colour.",
        )
        corrected_payload = json.loads(correction.split("\n", 1)[1])
        self.assertEqual(corrected_payload["original_feature_request"], original)
        self.assertEqual(
            corrected_payload["previous_interpretation"],
            RESULT.model_dump(mode="json"),
        )
        self.assertEqual(
            corrected_payload["user_correction"],
            "Use the normal gem's colour.",
        )
        with self.assertRaises(ValueError):
            render_feature_refiner_input(original, previous_result=RESULT)
        with self.assertRaises(ValueError):
            render_feature_refiner_input(
                original,
                previous_result=RESULT,
                user_correction="   ",
            )

    async def test_typed_execution_uses_existing_boundary_and_records_telemetry(
        self,
    ) -> None:
        seen: list[str] = []

        async def execute(_agent: object, input_text: str) -> AgentCallResult:
            seen.append(input_text)
            return AgentCallResult(
                output=RESULT,
                usage=TokenUsage(
                    requests=1,
                    input_tokens=100,
                    output_tokens=50,
                    total_tokens=150,
                ),
            )

        clock_values = iter([2.0, 2.25])
        with patch(
            "council.refinement.create_feature_refiner_agent",
            return_value="gpt-5.4-nano",
        ):
            execution = await run_feature_refiner(
                "Original feature",
                agent_executor=execute,
                clock=lambda: next(clock_values),
            )

        self.assertEqual(execution.result, RESULT)
        self.assertEqual(execution.telemetry.duration_ms, 250)
        self.assertEqual(execution.telemetry.usage.total_tokens, 150)
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(execution.estimated_cost_usd)

    async def test_execution_failure_is_wrapped_without_retry(self) -> None:
        calls = 0

        async def fail(_agent: object, _input: str) -> FeatureRefinerResult:
            nonlocal calls
            calls += 1
            raise RuntimeError("provider failed")

        with patch(
            "council.refinement.create_feature_refiner_agent",
            return_value="gpt-5.4-nano",
        ):
            with self.assertRaisesRegex(
                FeatureRefinementError,
                "provider failed",
            ):
                await run_feature_refiner(
                    "Original feature",
                    agent_executor=fail,
                )
        self.assertEqual(calls, 1)

    def test_record_aggregates_round_history_without_hidden_reasoning(self) -> None:
        first = _execution(RESULT, Decimal("0.001"), 100)
        second_result = RESULT.model_copy(
            update={"refined_brief": "Corrected canonical brief"}
        )
        second = _execution(second_result, Decimal("0.002"), 200)
        rounds = [
            refinement_round(first, user_correction=None),
            refinement_round(second, user_correction="Correct the colour rule."),
        ]

        record = create_feature_refinement_record(
            "Original feature",
            rounds,
            approved=True,
        )

        self.assertEqual(record.refinement_rounds, 2)
        self.assertEqual(record.attempted_calls, 2)
        self.assertTrue(record.usage_complete)
        self.assertEqual(record.approved_refined_feature, "Corrected canonical brief")
        self.assertEqual(record.telemetry.usage.requests, 2)
        self.assertEqual(record.telemetry.usage.total_tokens, 300)
        self.assertEqual(record.estimated_cost_usd, Decimal("0.003"))
        serialized = record.model_dump_json()
        self.assertNotIn("reasoning", serialized.casefold())
        self.assertNotIn("chain_of_thought", serialized.casefold())

    def test_old_refinement_record_without_completeness_fields_stays_readable(
        self,
    ) -> None:
        record = create_feature_refinement_record(
            "Original feature",
            [refinement_round(_execution(RESULT, Decimal("0.001"), 100), user_correction=None)],
            approved=True,
        )
        legacy = record.model_dump(mode="json")
        legacy.pop("attempted_calls")
        legacy.pop("usage_complete")
        legacy.pop("usage_unavailable_reason")

        restored = FeatureRefinementRecord.model_validate(legacy)

        self.assertTrue(restored.usage_complete)
        self.assertEqual(restored.attempted_calls, 1)

    def test_prompt_contract_is_interpretation_only(self) -> None:
        prompt = Path("prompts/feature_refiner.md").read_text(encoding="utf-8")
        contract = " ".join(prompt.casefold().split())
        for required in (
            "untrusted user-provided data",
            "do not inspect, imply, or claim repository facts",
            "implementation steps",
            "product decisions",
            "not specified by user",
        ):
            self.assertIn(required, contract)


def _execution(
    result: FeatureRefinerResult,
    cost: Decimal | None,
    total_tokens: int,
) -> FeatureRefinementExecution:
    return FeatureRefinementExecution(
        result=result,
        telemetry=RoleTelemetry(
            model="gpt-5.4-nano",
            duration_ms=100,
            usage=TokenUsage(
                requests=1,
                input_tokens=total_tokens // 2,
                output_tokens=total_tokens - total_tokens // 2,
                total_tokens=total_tokens,
            ),
        ),
        estimated_cost_usd=cost,
        unpriced_models=[] if cost is not None else ["gpt-unpriced"],
        pricing_snapshot_id="openai-2026-08-28",
    )


if __name__ == "__main__":
    unittest.main()
