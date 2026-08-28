import asyncio

from agents import Runner

from council.agents import create_director_agent
from council.models import (
    Confidence,
    EffortRange,
    EventSpec,
    ExperimentDefinition,
    OutcomeInterpretation,
    ProducerResult,
)

from test_producer_agent import (
    ANALYTICS,
    FEATURE,
    GAME_DESIGN,
    SCOPE_RISK,
    TECHNICAL,
)


PRODUCER = ProducerResult(
    confidence=Confidence.LOW,
    confidence_reason=(
        "A coherent experiment can be proposed, but repository evidence "
        "and implementation effort are unavailable."
    ),
    agreements=[
        "Prototype-first validation is appropriate.",
        "The core behavioural outcome must be observable.",
        "The full feature should not be built before the core assumption is tested.",
    ],
    disagreements=[
        "Specialists disagree about the minimum experiment duration and fidelity.",
    ],
    dependencies=[
        "The experiment requires reliable state across its observation window.",
        "The intended behavioural outcome must be measurable.",
    ],
    scope_conflicts=[
        "A richer experiment may improve representativeness but increases scope.",
    ],
    risk_conflicts=[
        "Reducing scope too far may reduce the credibility of the learning.",
    ],
    unknowns=[
        "Whether the reduced experiment is representative enough.",
        "Whether the proposed player value is strong enough to influence behaviour.",
    ],
    missing_repository_information=[
        "Existing implementation capabilities cannot be verified.",
        "Existing instrumentation capabilities cannot be verified.",
    ],
    human_decisions_required=[
        "Director/Product decision required on acceptable experiment representativeness.",
        "Director/Product decision required on decision thresholds.",
    ],
    proposed_experiment=ExperimentDefinition(
        player_problem=(
            "Players may lack sufficient motivation to perform "
            "the target repeat behaviour."
        ),
        hypothesis=(
            "If a meaningful consequence is clearly communicated, "
            "players may change their subsequent behaviour."
        ),
        riskiest_assumption=(
            "Players understand and value the proposed consequence enough "
            "for it to influence behaviour."
        ),
        smallest_experiment=(
            "A minimal controlled experiment containing one player decision "
            "followed by one observable consequence."
        ),
        build=[
            "Minimal player decision experience.",
            "Observable consequence.",
            "Conceptual measurement for exposure, behaviour, and outcome.",
        ],
        do_not_build=[
            "Full production feature.",
            "Long-term progression.",
            "Additional mechanics unrelated to the core hypothesis.",
        ],
        behavioural_question=(
            "Does the proposed consequence change subsequent player behaviour?"
        ),
        events=[
            EventSpec(
                event="ExperimentExposure",
                purpose="Observe experiment exposure.",
                properties=["variant"],
            ),
            EventSpec(
                event="ExperimentBehaviour",
                purpose="Observe target player behaviour.",
                properties=["variant", "behaviour_state"],
            ),
            EventSpec(
                event="ExperimentOutcome",
                purpose="Observe the consequence and subsequent outcome.",
                properties=["variant", "outcome_state"],
            ),
        ],
        funnel=[
            "Experiment exposed",
            "Player makes target decision",
            "Consequence becomes observable",
            "Subsequent behaviour observed",
        ],
        success_metrics=[
            "Target behaviour by experiment variant.",
            "Subsequent behaviour by experiment variant.",
        ],
        failure_signals=[
            "No interpretable behavioural difference.",
            "Experiment consequence is applied unreliably.",
        ],
        observation_window=(
            "Director/Product decision required based on the intended behaviour."
        ),
        segments=[
            "Experiment variant",
        ],
        learning_cases=[
            OutcomeInterpretation(
                observed_pattern=(
                    "The treatment shows stronger target behaviour "
                    "with reliable experiment execution."
                ),
                interpretation=(
                    "Supports further validation of the hypothesis."
                ),
                action="scale",
            ),
            OutcomeInterpretation(
                observed_pattern=(
                    "The experiment implementation or measurement is unreliable."
                ),
                interpretation=(
                    "The behavioural result cannot be interpreted."
                ),
                action="need_more_data",
            ),
        ],
        decision_threshold_notes=[
            "Director/Product decision required.",
        ],
    ),
    execution_steps=[
        "Prepare the minimal controlled experiment.",
        "Run the experiment.",
        "Measure target behaviour and consequence outcomes.",
        "Validate experiment integrity.",
        "Review the learning before further investment.",
    ],
    effort=EffortRange(
        developer_days_min=None,
        developer_days_max=None,
        basis=(
            "Repository evidence is unavailable, so implementation effort "
            "cannot be estimated credibly."
        ),
        confidence=Confidence.LOW,
    ),
    evidence_ids=[],
)


def build_director_input() -> str:
    return f"""
FEATURE
=======
{FEATURE}

GAME DESIGN RESULT
==================
{GAME_DESIGN.model_dump_json(indent=2)}

TECHNICAL RESULT
================
{TECHNICAL.model_dump_json(indent=2)}

ANALYTICS RESULT
================
{ANALYTICS.model_dump_json(indent=2)}

SCOPE / RISK RESULT
===================
{SCOPE_RISK.model_dump_json(indent=2)}

PRODUCER RESULT
===============
{PRODUCER.model_dump_json(indent=2)}

DIRECTOR TASK
=============
Judge the Producer's proposed experiment.

Do not redo the specialist analyses.

Judge expected validated learning against effort, evidence quality,
risk, specialist disagreement, and unresolved uncertainty.

Do not invent repository facts, implementation effort,
numeric thresholds, or production approval.
"""


async def main():
    agent = create_director_agent()

    print("Running Game Director...\n")

    director_input = build_director_input()

    result = await Runner.run(
        agent,
        director_input,
    )

    output = result.final_output

    print("=== DIRECTOR RESULT ===")
    print(output.model_dump_json(indent=2))

    print("\n=== DECISION ===")
    print(output.decision.value)

    print("\n=== CONFIDENCE ===")
    print(output.confidence.value)
    print(output.confidence_reason)

    print("\n=== RATIONALE ===")
    for item in output.rationale:
        print(f"- {item}")

    print("\n=== UNRESOLVED UNKNOWNS ===")
    for item in output.unresolved_unknowns:
        print(f"- {item}")

    print("\n=== HUMAN DECISIONS REQUIRED ===")
    for item in output.human_decisions_required:
        print(f"- {item}")

    print("\n=== DECISION CONDITIONS ===")

    print("Scale:")
    for item in output.decision_conditions.scale:
        print(f"- {item}")

    print("Iterate:")
    for item in output.decision_conditions.iterate:
        print(f"- {item}")

    print("Kill:")
    for item in output.decision_conditions.kill:
        print(f"- {item}")

    print("Need more data:")
    for item in output.decision_conditions.need_more_data:
        print(f"- {item}")

    print("\n=== HUMAN APPROVAL REQUIRED ===")
    print(output.human_approval_required)

    usage = result.context_wrapper.usage

    print("\n=== USAGE ===")
    print(f"Requests: {usage.requests}")
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print(f"Total tokens: {usage.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())