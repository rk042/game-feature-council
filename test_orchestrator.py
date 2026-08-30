import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import council.orchestrator as orchestrator_module
from council.models import (
    ContextBuilderConfig,
    ContextBundle,
    Confidence,
    CouncilResult,
    DecisionConditions,
    DirectorResult,
    EvidenceItem,
    EvidenceResolverResult,
    ResolvedConcern,
    ResolutionStatus,
    EvidenceLookupRequest,
    SupplementalRepositoryEvidence,
    EvidenceType,
    Finding,
    RepositoryEvidence,
    TokenUsage,
)
from council.orchestrator import (
    AgentCallResult,
    CouncilOrchestrationError,
    render_evidence_resolver_input,
    render_specialist_input,
    run_council,
    run_council_with_telemetry,
    validate_director_evidence,
    validate_producer_evidence,
    validate_specialist_evidence,
)
from council.evidence_resolver import collect_source_concerns
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
    "resolver_pass_1": EvidenceResolverResult(),
    "producer": PRODUCER,
    "director": DIRECTOR,
}


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_null_effort_medium_normalizes_to_low(self) -> None:
        result = await self._run_with_director_effort(
            minimum=None,
            maximum=None,
            confidence=Confidence.MEDIUM,
            basis="Arbitrary basis text.",
        )

        self.assertIsNone(result.director.effort.developer_days_min)
        self.assertIsNone(result.director.effort.developer_days_max)
        self.assertEqual(result.director.effort.confidence, Confidence.LOW)

    async def test_director_null_effort_high_normalizes_to_low(self) -> None:
        result = await self._run_with_director_effort(
            minimum=None,
            maximum=None,
            confidence=Confidence.HIGH,
            basis="No implementation is required.",
        )

        self.assertEqual(result.director.effort.confidence, Confidence.LOW)

    async def test_director_null_effort_low_remains_low(self) -> None:
        result = await self._run_with_director_effort(
            minimum=None,
            maximum=None,
            confidence=Confidence.LOW,
            basis="The estimate is unavailable.",
        )

        self.assertEqual(result.director.effort.confidence, Confidence.LOW)

    async def test_director_zero_effort_medium_remains_medium(self) -> None:
        result = await self._run_with_director_effort(
            minimum=0,
            maximum=0,
            confidence=Confidence.MEDIUM,
            basis="No developer implementation work is required.",
        )

        self.assertEqual(result.director.effort.confidence, Confidence.MEDIUM)

    async def test_director_zero_effort_high_remains_high(self) -> None:
        result = await self._run_with_director_effort(
            minimum=0,
            maximum=0,
            confidence=Confidence.HIGH,
            basis="No developer implementation work is required.",
        )

        self.assertEqual(result.director.effort.confidence, Confidence.HIGH)

    async def test_director_numeric_effort_keeps_confidence(self) -> None:
        result = await self._run_with_director_effort(
            minimum=2,
            maximum=5,
            confidence=Confidence.MEDIUM,
            basis="Repository evidence supports this range.",
        )

        self.assertEqual(result.director.effort.confidence, Confidence.MEDIUM)

    async def test_director_effort_normalization_ignores_basis_wording(
        self,
    ) -> None:
        results = [
            await self._run_with_director_effort(
                minimum=None,
                maximum=None,
                confidence=Confidence.HIGH,
                basis=basis,
            )
            for basis in (
                "No implementation is required.",
                "Insufficient evidence for an estimate.",
                "任意の説明文です。",
            )
        ]

        self.assertEqual(
            [result.director.effort.confidence for result in results],
            [Confidence.LOW, Confidence.LOW, Confidence.LOW],
        )

    def test_effort_prompts_share_null_and_zero_contract(self) -> None:
        for prompt_path in (
            "prompts/technical.md",
            "prompts/producer.md",
            "prompts/director.md",
            "prompts/generalist.md",
        ):
            with self.subTest(prompt=prompt_path):
                prompt = Path(prompt_path).read_text(encoding="utf-8")
                self.assertIn(
                    "`developer_days_min = null` and "
                    "`developer_days_max = null` mean the",
                    prompt,
                )
                self.assertIn("numeric estimate is unavailable", prompt)
                self.assertTrue(
                    "effort confidence must be low" in prompt
                    or "`effort.confidence` must be `low`" in prompt
                )
                self.assertIn(
                    "`developer_days_min = 0` and "
                    "`developer_days_max = 0`",
                    prompt,
                )

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

            if agent == "resolver_pass_1":
                self.assertEqual(set(completed), SPECIALIST_ROLES)
                return OUTPUTS[agent]

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

    def test_downstream_validation_accepts_only_actual_supplemental_ids(self) -> None:
        context = self._context("repo-001")
        producer = PRODUCER.model_copy(
            update={"evidence_ids": ["repo-001", "resolver-repo-001"]}
        )
        director = DIRECTOR.model_copy(
            update={"evidence_ids": ["resolver-repo-001"]}
        )

        validate_producer_evidence(
            context,
            producer,
            supplemental_evidence_ids=["resolver-repo-001"],
        )
        validate_director_evidence(
            context,
            director,
            supplemental_evidence_ids=["resolver-repo-001"],
        )
        with self.assertRaisesRegex(CouncilOrchestrationError, "resolver-repo-999"):
            validate_producer_evidence(
                context,
                producer.model_copy(
                    update={"evidence_ids": ["resolver-repo-999"]}
                ),
                supplemental_evidence_ids=["resolver-repo-001"],
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

    async def test_resolver_invalid_evidence_stops_producer_and_director(self) -> None:
        calls: list[str] = []
        invalid_resolver = EvidenceResolverResult(
            concerns=[
                ResolvedConcern(
                    concern_id="resolver-001",
                    kind="unknown",
                    canonical_concern="Repository question",
                    source_concern_ids=["technical-001"],
                    status=ResolutionStatus.RESOLVED_FROM_REPOSITORY,
                    resolution="Claimed answer.",
                    evidence_ids=["repo-999"],
                )
            ]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "resolver_pass_1":
                return invalid_resolver
            return OUTPUTS[agent]

        with self._patched_agent_factories():
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "Evidence Resolver result.*repo-999",
            ):
                await run_council(FEATURE, self._context(), execute)

        self.assertNotIn("producer", calls)
        self.assertNotIn("director", calls)

    async def test_resolver_rejects_fabricated_descriptive_source_id(self) -> None:
        calls: list[str] = []
        invalid = EvidenceResolverResult(
            concerns=[
                ResolvedConcern(
                    concern_id="resolver-001",
                    kind="unknown",
                    canonical_concern="Unmapped activation concern",
                    source_concern_ids=["game-design-unknown-activation"],
                    status=ResolutionStatus.HUMAN_REPOSITORY_HELP,
                    why_unresolved="The supplied concerns do not establish activation behaviour.",
                    human_question="Which tracked source owns activation?",
                )
            ]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            return invalid if agent == "resolver_pass_1" else OUTPUTS[agent]

        with self._patched_agent_factories(), patch.object(
            orchestrator_module,
            "bounded_targeted_lookup",
        ) as lookup:
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "Unknown ID:\\ngame-design-unknown-activation",
            ) as raised:
                await run_council(FEATURE, self._context(), execute)

        lookup.assert_not_called()
        self.assertIn("preserve evidence provenance", str(raised.exception))
        self.assertNotIn("producer", calls)
        self.assertNotIn("director", calls)

    async def test_resolver_rejects_fabricated_numeric_lookup_id(self) -> None:
        calls: list[str] = []
        invalid = EvidenceResolverResult(
            lookup_requests=[
                EvidenceLookupRequest(
                    concern_ids=["game-design-999"],
                    search_terms=["activation"],
                )
            ]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            return invalid if agent == "resolver_pass_1" else OUTPUTS[agent]

        with self._patched_agent_factories():
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "Unknown ID:\\ngame-design-999",
            ):
                await run_council(FEATURE, self._context(), execute)

        self.assertEqual(calls.count("resolver_pass_1"), 1)
        self.assertNotIn("producer", calls)
        self.assertNotIn("director", calls)

    async def test_valid_consolidated_source_ids_proceed_unchanged(self) -> None:
        calls: list[str] = []
        source_ids = [
            "game-design-003",
            "technical-002",
            "analytics-004",
            "scope-risk-003",
        ]
        game_design = GAME_DESIGN.model_copy(
            update={"risks": ["r1", "r2"], "unknowns": ["u3"]}
        )
        technical = TECHNICAL.model_copy(
            update={"risks": ["r1"], "unknowns": ["u2"]}
        )
        analytics = ANALYTICS.model_copy(
            update={"risks": ["r1", "r2", "r3"], "unknowns": ["u4"]}
        )
        scope_risk = SCOPE_RISK.model_copy(
            update={"risks": ["r1", "r2"], "unknowns": ["u3"]}
        )
        original_sources = collect_source_concerns(
            game_design, technical, analytics, scope_risk
        )
        resolver = EvidenceResolverResult(
            concerns=[
                ResolvedConcern(
                    concern_id="resolver-001",
                    kind="unknown",
                    canonical_concern="Consolidated concern",
                    source_concern_ids=source_ids,
                    status=ResolutionStatus.REQUIRES_EXPERIMENT,
                    why_unresolved="It requires observed player behaviour.",
                    how_to_answer="Run the bounded experiment.",
                )
            ]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            outputs = {
                "game_design": game_design,
                "technical": technical,
                "analytics": analytics,
                "scope_risk": scope_risk,
                "resolver_pass_1": resolver,
                "producer": PRODUCER,
                "director": DIRECTOR,
            }
            return outputs[agent]

        with self._patched_agent_factories():
            result = await run_council(FEATURE, self._context(), execute)

        self.assertEqual(result.evidence_resolver.concerns[0].source_concern_ids, source_ids)
        self.assertEqual(result.evidence_resolver.source_concerns, original_sources)
        self.assertIn("producer", calls)
        self.assertIn("director", calls)

    def test_resolver_input_exposes_the_exact_opaque_source_id_registry(self) -> None:
        concerns = collect_source_concerns(
            GAME_DESIGN, TECHNICAL, ANALYTICS, SCOPE_RISK
        )
        rendered = render_evidence_resolver_input(
            FEATURE,
            self._context(),
            concerns,
            GAME_DESIGN,
            TECHNICAL,
            ANALYTICS,
            SCOPE_RISK,
        )

        self.assertIn("ALLOWED SOURCE CONCERN IDS (OPAQUE IDENTIFIERS)", rendered)
        self.assertIn("Copy only exact IDs from this registry", rendered)
        self.assertIn("Never invent or transform an ID", rendered)
        for concern in concerns:
            self.assertIn(concern.id, rendered)

    def test_resolver_prompt_marks_source_ids_as_opaque(self) -> None:
        prompt = Path("prompts/evidence_resolver.md").read_text(encoding="utf-8")

        self.assertIn("SOURCE CONCERN IDS ARE OPAQUE IDENTIFIERS", prompt)
        self.assertIn("Copy only exact IDs supplied", prompt)
        self.assertIn("Never invent, transform", prompt)
        self.assertIn("Repository lookup always", prompt)
        self.assertIn("comes before asking a human for repository help", prompt)

    async def test_lookup_runs_second_resolver_pass_before_producer(self) -> None:
        calls: list[str] = []
        inputs: dict[str, list[str]] = {}
        first = EvidenceResolverResult(
            lookup_requests=[
                EvidenceLookupRequest(
                    concern_ids=["technical-001"],
                    search_terms=["match count"],
                )
            ]
        )
        final = EvidenceResolverResult(
            concerns=[
                ResolvedConcern(
                    concern_id="resolver-001",
                    kind="unknown",
                    canonical_concern="Which system owns match count",
                    source_concern_ids=["technical-001"],
                    status=ResolutionStatus.RESOLVED_FROM_REPOSITORY,
                    resolution="Tracked source exposes the match-count owner.",
                    evidence_ids=["resolver-repo-001"],
                )
            ]
        )

        async def execute(agent: str, input_text: str):
            calls.append(agent)
            inputs.setdefault(agent, []).append(input_text)
            if agent == "resolver_pass_1":
                return first if len(inputs[agent]) == 1 else final
            if agent == "producer":
                return PRODUCER.model_copy(
                    update={"evidence_ids": ["repo-001", "resolver-repo-001"]}
                )
            if agent == "director":
                return DIRECTOR.model_copy(
                    update={"evidence_ids": ["resolver-repo-001"]}
                )
            return OUTPUTS[agent]

        supplemental = SupplementalRepositoryEvidence(
            id="resolver-repo-001",
            file_path="src/Match.cs",
            matched_terms=["match count"],
            text="count",
            truncated=False,
        )
        context = self._context()
        original_context = context.model_copy(deep=True)
        with self._patched_agent_factories(), patch.object(
            orchestrator_module,
            "bounded_targeted_lookup",
            return_value=([supplemental], []),
        ):
            result = await run_council(FEATURE, context, execute)

        self.assertEqual(calls.count("resolver_pass_1"), 2)
        self.assertLess(calls.index("resolver_pass_1"), calls.index("producer"))
        self.assertIn("resolver-repo-001", inputs["producer"][0])
        self.assertIn("resolver-repo-001", inputs["director"][0])
        self.assertTrue(result.evidence_resolver.second_pass_occurred)
        self.assertEqual(context, original_context)
        self.assertEqual(result.context, original_context)
        self.assertEqual(result.producer.evidence_ids, ["repo-001", "resolver-repo-001"])
        self.assertEqual(result.director.evidence_ids, ["resolver-repo-001"])

    async def test_premature_repository_help_enforces_lookup_and_second_pass(self) -> None:
        calls: list[str] = []
        progress_events = []
        pass_one = EvidenceResolverResult(
            concerns=[self._human_repository_concern("resolver-001", ["technical-001"])]
        )
        pass_two = EvidenceResolverResult(
            concerns=[
                ResolvedConcern(
                    concern_id="resolver-001",
                    kind="unknown",
                    canonical_concern="Repository ownership",
                    source_concern_ids=["technical-001"],
                    status=ResolutionStatus.RESOLVED_FROM_REPOSITORY,
                    resolution="Supplemental evidence identifies the owner.",
                    evidence_ids=["resolver-repo-001"],
                )
            ]
        )
        supplemental = SupplementalRepositoryEvidence(
            id="resolver-repo-001",
            file_path="src/Owner.cs",
            matched_terms=["ownership"],
            text="owner",
            truncated=False,
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "resolver_pass_1":
                return pass_one if calls.count(agent) == 1 else pass_two
            if agent == "producer":
                return PRODUCER.model_copy(update={"evidence_ids": ["resolver-repo-001"]})
            if agent == "director":
                return DIRECTOR.model_copy(update={"evidence_ids": ["resolver-repo-001"]})
            return OUTPUTS[agent]

        with self._patched_agent_factories(), patch.object(
            orchestrator_module,
            "bounded_targeted_lookup",
            return_value=([supplemental], []),
        ) as lookup:
            result = await run_council(
                FEATURE,
                self._context(),
                execute,
                progress_listener=progress_events.append,
            )

        requests = lookup.call_args.args[1]
        self.assertEqual(calls.count("resolver_pass_1"), 2)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].concern_ids, ["technical-001"])
        self.assertEqual(result.evidence_resolver.lookup_requests, requests)
        self.assertTrue(
            any(
                event.role == "evidence_lookup"
                and event.message
                == "Workflow enforced repository lookup before human repository help."
                for event in progress_events
            )
        )

    async def test_existing_repository_help_lookup_is_not_duplicated(self) -> None:
        calls: list[str] = []
        request = EvidenceLookupRequest(
            concern_ids=["technical-001"],
            search_terms=["persistence"],
        )
        pass_one = EvidenceResolverResult(
            concerns=[self._human_repository_concern("resolver-001", ["technical-001"])],
            lookup_requests=[request],
        )
        pass_two = EvidenceResolverResult(
            concerns=[self._human_repository_concern("resolver-001", ["technical-001"])]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "resolver_pass_1":
                return pass_one if calls.count(agent) == 1 else pass_two
            return OUTPUTS[agent]

        with self._patched_agent_factories(), patch.object(
            orchestrator_module,
            "bounded_targeted_lookup",
            return_value=([], ["No tracked file matched."]),
        ) as lookup:
            result = await run_council(FEATURE, self._context(), execute)

        self.assertEqual(lookup.call_args.args[1], [request])
        self.assertEqual(result.evidence_resolver.lookup_requests, [request])
        self.assertEqual(result.evidence_resolver.concerns[0].status, ResolutionStatus.HUMAN_REPOSITORY_HELP)

    async def test_multiple_premature_repository_help_concerns_merge_bounded_lookup(self) -> None:
        calls: list[str] = []
        pass_one = EvidenceResolverResult(
            concerns=[
                self._human_repository_concern("resolver-001", ["game-design-001", "technical-001"]),
                self._human_repository_concern("resolver-002", ["analytics-001", "scope-risk-001"]),
            ]
        )
        pass_two = EvidenceResolverResult(
            concerns=[
                self._human_repository_concern("resolver-001", ["game-design-001", "technical-001"]),
                self._human_repository_concern("resolver-002", ["analytics-001", "scope-risk-001"]),
            ]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "resolver_pass_1":
                return pass_one if calls.count(agent) == 1 else pass_two
            return OUTPUTS[agent]

        with self._patched_agent_factories(), patch.object(
            orchestrator_module,
            "bounded_targeted_lookup",
            return_value=([], ["No tracked file matched."]),
        ) as lookup:
            result = await run_council(FEATURE, self._context(), execute)

        requests = lookup.call_args.args[1]
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0].concern_ids,
            ["game-design-001", "technical-001", "analytics-001", "scope-risk-001"],
        )
        self.assertLessEqual(len(requests[0].search_terms), 5)
        self.assertEqual(result.evidence_resolver.lookup_requests, requests)

    async def test_enforced_lookup_stays_within_saturated_request_limit(self) -> None:
        calls: list[str] = []
        existing = [
            EvidenceLookupRequest(
                concern_ids=[source_id],
                search_terms=[term],
            )
            for source_id, term in (
                ("game-design-001", "design"),
                ("technical-001", "persistence"),
                ("scope-risk-001", "scope"),
            )
        ]
        pass_one = EvidenceResolverResult(
            concerns=[self._human_repository_concern("resolver-001", ["analytics-001"])],
            lookup_requests=existing,
        )
        pass_two = EvidenceResolverResult(
            concerns=[self._human_repository_concern("resolver-001", ["analytics-001"])]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "resolver_pass_1":
                return pass_one if calls.count(agent) == 1 else pass_two
            return OUTPUTS[agent]

        with self._patched_agent_factories(), patch.object(
            orchestrator_module,
            "bounded_targeted_lookup",
            return_value=([], ["No tracked file matched."]),
        ) as lookup:
            result = await run_council(FEATURE, self._context(), execute)

        requests = lookup.call_args.args[1]
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[:2], existing[:2])
        self.assertEqual(
            requests[2].concern_ids,
            ["scope-risk-001", "analytics-001"],
        )
        self.assertIn("scope", requests[2].search_terms)
        self.assertLessEqual(len(requests[2].search_terms), 5)
        self.assertEqual(result.evidence_resolver.lookup_requests, requests)

    async def test_final_repository_help_requires_lookup_for_that_concern(self) -> None:
        calls: list[str] = []
        pass_one = EvidenceResolverResult(
            concerns=[self._human_repository_concern("resolver-001", ["technical-001"])]
        )
        pass_two = EvidenceResolverResult(
            concerns=[self._human_repository_concern("resolver-002", ["analytics-001"])]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            if agent == "resolver_pass_1":
                return pass_one if calls.count(agent) == 1 else pass_two
            return OUTPUTS[agent]

        with self._patched_agent_factories(), patch.object(
            orchestrator_module,
            "bounded_targeted_lookup",
            return_value=([], ["No tracked file matched."]),
        ):
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "lookup-before-human invariant",
            ):
                await run_council(FEATURE, self._context(), execute)

        self.assertNotIn("producer", calls)
        self.assertNotIn("director", calls)

    async def test_product_and_experiment_statuses_do_not_force_lookup(self) -> None:
        calls: list[str] = []
        resolver = EvidenceResolverResult(
            concerns=[
                ResolvedConcern(
                    concern_id="resolver-001",
                    kind="unknown",
                    canonical_concern="Product choice",
                    source_concern_ids=["game-design-001"],
                    status=ResolutionStatus.HUMAN_PRODUCT_DECISION,
                    why_unresolved="Product intent is not stored in source.",
                    human_question="Which behavior is intended?",
                ),
                ResolvedConcern(
                    concern_id="resolver-002",
                    kind="unknown",
                    canonical_concern="Player response",
                    source_concern_ids=["analytics-001"],
                    status=ResolutionStatus.REQUIRES_EXPERIMENT,
                    why_unresolved="Static source cannot establish player response.",
                    how_to_answer="Run the bounded experiment.",
                ),
            ]
        )

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            return resolver if agent == "resolver_pass_1" else OUTPUTS[agent]

        with self._patched_agent_factories(), patch.object(
            orchestrator_module,
            "bounded_targeted_lookup",
        ) as lookup:
            await run_council(FEATURE, self._context(), execute)

        lookup.assert_not_called()
        self.assertEqual(calls.count("resolver_pass_1"), 1)
        self.assertIn("producer", calls)
        self.assertIn("director", calls)

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
            create_evidence_resolver_agent=lambda: "resolver_pass_1",
            create_producer_agent=lambda: "producer",
            create_director_agent=lambda: "director",
        )

    async def _run_with_director_effort(
        self,
        *,
        minimum: float | None,
        maximum: float | None,
        confidence: Confidence,
        basis: str,
    ) -> CouncilResult:
        director = DIRECTOR.model_copy(
            update={
                "effort": DIRECTOR.effort.model_copy(
                    update={
                        "developer_days_min": minimum,
                        "developer_days_max": maximum,
                        "basis": basis,
                        "confidence": confidence,
                    }
                )
            }
        )

        async def execute(agent: str, _input_text: str):
            if agent == "director":
                return director
            return OUTPUTS[agent]

        with self._patched_agent_factories():
            return await run_council(FEATURE, self._context(), execute)

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

    @staticmethod
    def _human_repository_concern(
        concern_id: str,
        source_ids: list[str],
    ) -> ResolvedConcern:
        return ResolvedConcern(
            concern_id=concern_id,
            kind="unknown",
            canonical_concern=f"Repository location for {concern_id}",
            source_concern_ids=source_ids,
            status=ResolutionStatus.HUMAN_REPOSITORY_HELP,
            why_unresolved="The supplied evidence does not identify the location.",
            human_question="Which tracked source owns this behavior?",
        )


if __name__ == "__main__":
    unittest.main()
