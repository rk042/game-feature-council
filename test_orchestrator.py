import asyncio
import unittest
from unittest.mock import patch

import council.orchestrator as orchestrator_module
from council.models import (
    ContextBuilderConfig,
    ContextBundle,
    CouncilResult,
    DecisionConditions,
    DirectorResult,
    EvidenceItem,
    EvidenceType,
    Finding,
    RepositoryEvidence,
    TokenUsage,
)
from council.orchestrator import (
    AgentCallResult,
    CouncilOrchestrationError,
    render_specialist_input,
    run_council,
    run_council_with_telemetry,
    validate_producer_evidence,
    validate_specialist_evidence,
)
from test_director_agent import PRODUCER
from test_producer_agent import (
    ANALYTICS,
    FEATURE,
    GAME_DESIGN,
    SCOPE_RISK,
    TECHNICAL,
)


DIRECTOR = DirectorResult(
    decision="PROTOTYPE_FIRST",
    confidence="medium",
    confidence_reason="The synthetic evidence supports a bounded prototype.",
    rationale=["Validate the core assumption before expanding scope."],
    experiment=PRODUCER.proposed_experiment,
    effort=PRODUCER.effort,
    unresolved_unknowns=["Synthetic decision threshold is unspecified."],
    human_decisions_required=["Choose a decision threshold."],
    decision_conditions=DecisionConditions(
        scale=["Observed behaviour supports the hypothesis."],
        iterate=["The experience needs a bounded adjustment."],
        kill=["Observed behaviour rejects the hypothesis."],
        need_more_data=["Experiment integrity is inconclusive."],
    ),
)

SPECIALIST_ROLES = {
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
}
OUTPUTS = {
    "game_design": GAME_DESIGN,
    "technical": TECHNICAL,
    "analytics": ANALYTICS,
    "scope_risk": SCOPE_RISK,
    "producer": PRODUCER,
    "director": DIRECTOR,
}


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_concurrent_fan_out_and_sequential_fan_in(self) -> None:
        context = self._context()
        started: set[str] = set()
        completed: list[str] = []
        calls: list[str] = []
        inputs: dict[str, str] = {}
        releases = {role: asyncio.Event() for role in SPECIALIST_ROLES}
        completion_events = {
            role: asyncio.Event()
            for role in SPECIALIST_ROLES
        }
        all_specialists_started = asyncio.Event()
        producer_completed = False

        async def execute(agent: str, input_text: str):
            nonlocal producer_completed
            calls.append(agent)
            inputs[agent] = input_text

            if agent in SPECIALIST_ROLES:
                started.add(agent)
                if started == SPECIALIST_ROLES:
                    all_specialists_started.set()
                await releases[agent].wait()
                completed.append(agent)
                completion_events[agent].set()
                return OUTPUTS[agent]

            if agent == "producer":
                self.assertEqual(set(completed), SPECIALIST_ROLES)
                producer_completed = True
                return PRODUCER

            if agent == "director":
                self.assertTrue(producer_completed)
                return DIRECTOR

            raise AssertionError(f"Unexpected agent: {agent}")

        with self._patched_agent_factories():
            council_task = asyncio.create_task(
                run_council(FEATURE, context, execute)
            )
            await asyncio.wait_for(all_specialists_started.wait(), timeout=2)

            self.assertEqual(started, SPECIALIST_ROLES)
            self.assertNotIn("producer", calls)
            self.assertNotIn("director", calls)

            completion_order = [
                "scope_risk",
                "analytics",
                "technical",
                "game_design",
            ]
            for role in completion_order:
                releases[role].set()
                await asyncio.wait_for(
                    completion_events[role].wait(),
                    timeout=2,
                )

            result = await asyncio.wait_for(council_task, timeout=2)

        self.assertEqual(completed, completion_order)
        self.assertEqual(
            {role: calls.count(role) for role in OUTPUTS},
            {role: 1 for role in OUTPUTS},
        )
        specialist_inputs = {inputs[role] for role in SPECIALIST_ROLES}
        self.assertEqual(len(specialist_inputs), 1)

        for specialist in (GAME_DESIGN, TECHNICAL, ANALYTICS, SCOPE_RISK):
            serialized = specialist.model_dump_json(indent=2)
            self.assertIn(serialized, inputs["producer"])
            self.assertIn(serialized, inputs["director"])
        self.assertIn(PRODUCER.model_dump_json(indent=2), inputs["director"])

        self.assertIsInstance(result, CouncilResult)
        self.assertEqual(result.context, context)
        self.assertEqual(result.game_design, GAME_DESIGN)
        self.assertEqual(result.technical, TECHNICAL)
        self.assertEqual(result.analytics, ANALYTICS)
        self.assertEqual(result.scope_risk, SCOPE_RISK)
        self.assertEqual(result.producer, PRODUCER)
        self.assertEqual(result.director, DIRECTOR)

    async def test_specialist_failure_stops_synthesis(self) -> None:
        calls: list[str] = []

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "technical":
                raise RuntimeError("synthetic specialist failure")
            return OUTPUTS[agent]

        with self._patched_agent_factories():
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "Specialist phase failed.*Technical specialist",
            ):
                await run_council(FEATURE, self._context(), execute)

        self.assertNotIn("producer", calls)
        self.assertNotIn("director", calls)

    async def test_producer_failure_stops_director(self) -> None:
        calls: list[str] = []

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "producer":
                raise RuntimeError("synthetic producer failure")
            return OUTPUTS[agent]

        with self._patched_agent_factories():
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "Producer execution failed",
            ):
                await run_council(FEATURE, self._context(), execute)

        self.assertEqual(calls.count("producer"), 1)
        self.assertNotIn("director", calls)

    def test_producer_evidence_accepts_valid_and_empty_ids(self) -> None:
        context = self._context("repo-001")

        validate_producer_evidence(context, PRODUCER)
        validate_producer_evidence(
            context,
            PRODUCER.model_copy(update={"evidence_ids": ["repo-001"]}),
        )

    async def test_invalid_producer_evidence_stops_director(self) -> None:
        calls: list[str] = []
        invalid_producer = PRODUCER.model_copy(
            update={"evidence_ids": ["repo-999"]}
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "producer":
                return invalid_producer
            return OUTPUTS[agent]

        with self._patched_agent_factories():
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "Producer result.*repo-999",
            ):
                await run_council(FEATURE, self._context(), execute)

        self.assertEqual(calls.count("producer"), 1)
        self.assertNotIn("director", calls)

    async def test_invalid_repository_evidence_stops_before_producer(self) -> None:
        calls: list[str] = []
        invalid_game_design = GAME_DESIGN.model_copy(
            update={
                "findings": [
                    Finding(
                        statement="Synthetic repository claim.",
                        evidence_ids=["repo-999"],
                    )
                ]
            }
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "game_design":
                return invalid_game_design
            return OUTPUTS[agent]

        with self._patched_agent_factories():
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "game_design result.*repo-999",
            ):
                await run_council(FEATURE, self._context(), execute)

        self.assertNotIn("producer", calls)
        self.assertNotIn("director", calls)

    def test_repository_evidence_uses_exact_bundle_and_source_type(self) -> None:
        valid_game_design = self._game_design_with_evidence(
            "repo-001",
            EvidenceType.REPOSITORY,
        )
        validate_specialist_evidence(
            self._context("repo-001"),
            valid_game_design,
            TECHNICAL,
            ANALYTICS,
            SCOPE_RISK,
        )

        with self.assertRaisesRegex(
            CouncilOrchestrationError,
            "game_design result.*repo-001",
        ):
            validate_specialist_evidence(
                self._context("repo-002"),
                valid_game_design,
                TECHNICAL,
                ANALYTICS,
                SCOPE_RISK,
            )

        inference_game_design = self._game_design_with_evidence(
            "repo-999",
            EvidenceType.AGENT_INFERENCE,
        )
        validate_specialist_evidence(
            self._context("repo-001"),
            inference_game_design,
            TECHNICAL,
            ANALYTICS,
            SCOPE_RISK,
        )

    def test_specialist_input_rendering_is_deterministic_and_separated(self) -> None:
        context = self._context()

        first = render_specialist_input(FEATURE, context)
        second = render_specialist_input(FEATURE, context.model_copy(deep=True))

        self.assertEqual(first, second)
        self.assertIn("FEATURE / USER INPUT (NOT REPOSITORY EVIDENCE)", first)
        self.assertIn("REPOSITORY PROVENANCE", first)
        self.assertIn("BOUNDED REPOSITORY EVIDENCE", first)
        self.assertIn("repo-001", first)
        self.assertIn("src/ExampleFeatureService.py", first)
        self.assertIn("Synthetic repository excerpt.", first)

    async def test_execution_telemetry_aggregates_sdk_reported_usage(self) -> None:
        usage_by_role = {
            role: TokenUsage(
                requests=1,
                input_tokens=index * 100,
                output_tokens=index * 10,
                total_tokens=index * 110,
            )
            for index, role in enumerate(OUTPUTS, start=1)
        }

        async def execute(agent: str, _input_text: str):
            await asyncio.sleep(0)
            return AgentCallResult(
                output=OUTPUTS[agent],
                usage=usage_by_role[agent],
            )

        with self._patched_agent_factories():
            execution = await run_council_with_telemetry(
                FEATURE,
                self._context(),
                execute,
            )

        self.assertEqual(set(execution.telemetry.roles), set(OUTPUTS))
        self.assertTrue(
            all(
                role.duration_ms >= 0
                for role in execution.telemetry.roles.values()
            )
        )
        self.assertGreaterEqual(execution.telemetry.total_duration_ms, 0)
        self.assertEqual(
            execution.telemetry.total_usage.requests,
            sum(usage.requests for usage in usage_by_role.values()),
        )
        self.assertEqual(
            execution.telemetry.total_usage.input_tokens,
            sum(usage.input_tokens for usage in usage_by_role.values()),
        )
        self.assertEqual(
            execution.telemetry.total_usage.output_tokens,
            sum(usage.output_tokens for usage in usage_by_role.values()),
        )
        self.assertEqual(
            execution.telemetry.total_usage.total_tokens,
            sum(usage.total_tokens for usage in usage_by_role.values()),
        )

    def _patched_agent_factories(self):
        return patch.multiple(
            orchestrator_module,
            create_game_design_agent=lambda: "game_design",
            create_technical_agent=lambda: "technical",
            create_analytics_agent=lambda: "analytics",
            create_scope_risk_agent=lambda: "scope_risk",
            create_producer_agent=lambda: "producer",
            create_director_agent=lambda: "director",
        )

    def _context(self, evidence_id: str = "repo-001") -> ContextBundle:
        evidence = RepositoryEvidence(
            id=evidence_id,
            file_path="src/ExampleFeatureService.py",
            selection_reasons=["feature term match", "source relevance"],
            matched_terms=["repeat", "engagement"],
            text="Synthetic repository excerpt.\n",
            truncated=False,
        )
        return ContextBundle(
            repository_path="/synthetic/example-repository",
            commit_sha="1" * 40,
            branch="example-branch",
            working_tree_dirty=False,
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

    def _game_design_with_evidence(
        self,
        evidence_id: str,
        source_type: EvidenceType,
    ):
        evidence = EvidenceItem(
            id=evidence_id,
            source_type=source_type,
            claim="Synthetic evidence claim.",
            file_path="src/ExampleFeatureService.py",
            reason_it_matters="It informs the synthetic feature review.",
        )
        finding = Finding(
            statement="Synthetic finding.",
            evidence_ids=[evidence_id],
        )
        return GAME_DESIGN.model_copy(
            update={"evidence": [evidence], "findings": [finding]}
        )


if __name__ == "__main__":
    unittest.main()
