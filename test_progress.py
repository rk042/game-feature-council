import asyncio
import unittest
from unittest.mock import patch

from council.evaluation import EvaluationError, run_generalist
from council.models import TokenUsage
from council.orchestrator import (
    AgentCallResult,
    CouncilOrchestrationError,
    run_council,
    run_council_with_telemetry,
)
from council.progress import ProgressEvent, ProgressStatus
import test_orchestrator
from test_producer_agent import FEATURE


class _Agent:
    def __init__(self, name: str) -> None:
        self.model = name


class ProgressEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_specialist_start_listener_failure_is_observational(
        self,
    ) -> None:
        helper = test_orchestrator.OrchestratorTests()
        calls: list[str] = []

        async def execute(agent: str, _input_text: str):
            calls.append(agent)
            return test_orchestrator.OUTPUTS[agent]

        def listener(event: ProgressEvent) -> None:
            if event.status == ProgressStatus.STARTED:
                raise RuntimeError("synthetic start-listener failure")

        with helper._patched_agent_factories():
            result = await run_council(
                FEATURE,
                helper._context(),
                execute,
                progress_listener=listener,
            )

        self.assertEqual(result.director, test_orchestrator.DIRECTOR)
        self.assertEqual(set(calls), set(test_orchestrator.OUTPUTS))

    async def test_specialist_completion_listener_failure_preserves_telemetry(
        self,
    ) -> None:
        helper = test_orchestrator.OrchestratorTests()

        async def execute(agent: str, _input_text: str):
            return AgentCallResult(
                output=test_orchestrator.OUTPUTS[agent],
                usage=TokenUsage(
                    requests=1,
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                ),
            )

        def listener(event: ProgressEvent) -> None:
            if event.status == ProgressStatus.COMPLETED:
                raise RuntimeError("synthetic completion-listener failure")

        with helper._patched_agent_factories():
            execution = await run_council_with_telemetry(
                FEATURE,
                helper._context(),
                execute,
                progress_listener=listener,
            )

        self.assertEqual(execution.result.director, test_orchestrator.DIRECTOR)
        self.assertEqual(execution.telemetry.total_usage.requests, 6)
        self.assertEqual(execution.telemetry.total_usage.total_tokens, 90)

    async def test_specialist_failure_listener_cannot_mask_original_error(
        self,
    ) -> None:
        helper = test_orchestrator.OrchestratorTests()

        async def execute(agent: str, _input_text: str):
            await asyncio.sleep(0)
            if agent == "technical":
                raise RuntimeError("original provider failure")
            return test_orchestrator.OUTPUTS[agent]

        def listener(event: ProgressEvent) -> None:
            if event.status == ProgressStatus.FAILED:
                raise RuntimeError("listener must not escape")

        with helper._patched_agent_factories():
            with self.assertRaises(CouncilOrchestrationError) as raised:
                await run_council(
                    FEATURE,
                    helper._context(),
                    execute,
                    progress_listener=listener,
                )

        self.assertNotIsInstance(raised.exception, BaseExceptionGroup)
        message = str(raised.exception)
        self.assertIn("original provider failure", message)
        self.assertNotIn("listener must not escape", message)

    async def test_council_events_preserve_parallel_and_dependency_order(
        self,
    ) -> None:
        helper = test_orchestrator.OrchestratorTests()
        events: list[ProgressEvent] = []
        started: set[str] = set()
        all_started = asyncio.Event()
        release = asyncio.Event()

        async def execute(agent: str, _input_text: str):
            if agent in test_orchestrator.SPECIALIST_ROLES:
                started.add(agent)
                if started == test_orchestrator.SPECIALIST_ROLES:
                    all_started.set()
                await release.wait()
            return AgentCallResult(
                output=test_orchestrator.OUTPUTS[agent],
                usage=TokenUsage(
                    requests=1,
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                ),
            )

        with helper._patched_agent_factories():
            task = asyncio.create_task(
                run_council_with_telemetry(
                    FEATURE,
                    helper._context(),
                    execute,
                    progress_listener=events.append,
                )
            )
            await asyncio.wait_for(all_started.wait(), timeout=2)
            self.assertEqual(
                {
                    event.role
                    for event in events
                    if event.status == ProgressStatus.STARTED
                },
                test_orchestrator.SPECIALIST_ROLES,
            )
            self.assertFalse(
                any(
                    event.status == ProgressStatus.COMPLETED
                    for event in events
                )
            )
            release.set()
            await task

        specialist_completions = [
            index
            for index, event in enumerate(events)
            if event.role in test_orchestrator.SPECIALIST_ROLES
            and event.status == ProgressStatus.COMPLETED
        ]
        producer_started = events.index(
            next(
                event
                for event in events
                if event.role == "producer"
                and event.status == ProgressStatus.STARTED
            )
        )
        producer_completed = events.index(
            next(
                event
                for event in events
                if event.role == "producer"
                and event.status == ProgressStatus.COMPLETED
            )
        )
        director_started = events.index(
            next(
                event
                for event in events
                if event.role == "director"
                and event.status == ProgressStatus.STARTED
            )
        )
        self.assertLess(max(specialist_completions), producer_started)
        self.assertLess(producer_completed, director_started)
        completed = [
            event
            for event in events
            if event.status == ProgressStatus.COMPLETED
        ]
        self.assertTrue(all(event.usage is not None for event in completed))

    async def test_failure_event_precedes_clear_orchestration_error(self) -> None:
        helper = test_orchestrator.OrchestratorTests()
        events: list[ProgressEvent] = []

        async def execute(agent: str, _input_text: str):
            await asyncio.sleep(0)
            if agent == "technical":
                raise RuntimeError("synthetic provider failure")
            return test_orchestrator.OUTPUTS[agent]

        with helper._patched_agent_factories():
            with self.assertRaisesRegex(
                CouncilOrchestrationError,
                "Specialist phase failed.*synthetic provider failure",
            ):
                await run_council(
                    FEATURE,
                    helper._context(),
                    execute,
                    progress_listener=events.append,
                )

        self.assertTrue(
            any(
                event.role == "technical"
                and event.status == ProgressStatus.FAILED
                for event in events
            )
        )
        self.assertFalse(any(event.role == "producer" for event in events))
        self.assertFalse(any(event.role == "director" for event in events))

    async def test_generalist_is_a_separate_progress_role(self) -> None:
        helper = test_orchestrator.OrchestratorTests()
        events: list[ProgressEvent] = []

        async def execute(_agent: _Agent, _input_text: str):
            return AgentCallResult(
                output=test_orchestrator.DIRECTOR,
                usage=TokenUsage(requests=1, total_tokens=9),
            )

        with patch(
            "council.evaluation.create_generalist_agent",
            return_value=_Agent("gpt-5.4-nano"),
        ):
            await run_generalist(
                FEATURE,
                helper._context(),
                execute,
                progress_listener=events.append,
            )

        self.assertEqual(
            [(event.role, event.status) for event in events],
            [
                ("generalist", ProgressStatus.STARTED),
                ("generalist", ProgressStatus.COMPLETED),
            ],
        )

    async def test_generalist_failure_emits_failed_event(self) -> None:
        helper = test_orchestrator.OrchestratorTests()
        events: list[ProgressEvent] = []

        async def execute(_agent: _Agent, _input_text: str):
            raise RuntimeError("synthetic baseline failure")

        with patch(
            "council.evaluation.create_generalist_agent",
            return_value=_Agent("gpt-5.4-nano"),
        ):
            with self.assertRaisesRegex(
                EvaluationError,
                "synthetic baseline failure",
            ):
                await run_generalist(
                    FEATURE,
                    helper._context(),
                    execute,
                    progress_listener=events.append,
                )

        self.assertEqual(
            [(event.role, event.status) for event in events],
            [
                ("generalist", ProgressStatus.STARTED),
                ("generalist", ProgressStatus.FAILED),
            ],
        )

    async def test_generalist_listener_failure_is_observational_on_success(
        self,
    ) -> None:
        helper = test_orchestrator.OrchestratorTests()

        async def execute(_agent: _Agent, _input_text: str):
            return AgentCallResult(
                output=test_orchestrator.DIRECTOR,
                usage=TokenUsage(requests=1, total_tokens=9),
            )

        def listener(_event: ProgressEvent) -> None:
            raise RuntimeError("synthetic listener failure")

        with patch(
            "council.evaluation.create_generalist_agent",
            return_value=_Agent("gpt-5.4-nano"),
        ):
            execution = await run_generalist(
                FEATURE,
                helper._context(),
                execute,
                progress_listener=listener,
            )

        self.assertEqual(execution.result, test_orchestrator.DIRECTOR)
        self.assertEqual(execution.telemetry.usage.total_tokens, 9)

    async def test_generalist_listener_failure_cannot_mask_execution_error(
        self,
    ) -> None:
        helper = test_orchestrator.OrchestratorTests()

        async def execute(_agent: _Agent, _input_text: str):
            raise RuntimeError("original generalist provider failure")

        def listener(_event: ProgressEvent) -> None:
            raise RuntimeError("listener must not escape")

        with patch(
            "council.evaluation.create_generalist_agent",
            return_value=_Agent("gpt-5.4-nano"),
        ):
            with self.assertRaises(EvaluationError) as raised:
                await run_generalist(
                    FEATURE,
                    helper._context(),
                    execute,
                    progress_listener=listener,
                )

        message = str(raised.exception)
        self.assertIn("original generalist provider failure", message)
        self.assertNotIn("listener must not escape", message)

    async def test_runtime_remains_valid_without_listener(self) -> None:
        helper = test_orchestrator.OrchestratorTests()

        async def execute(agent: str, _input_text: str):
            return test_orchestrator.OUTPUTS[agent]

        with helper._patched_agent_factories():
            result = await run_council(
                FEATURE,
                helper._context(),
                execute,
            )
        self.assertEqual(result.director, test_orchestrator.DIRECTOR)


if __name__ == "__main__":
    unittest.main()
