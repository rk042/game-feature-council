import asyncio

from council.agents import create_producer_agent
from council.execution import execute_agent
from council.models import (
    AnalyticsResult,
    Confidence,
    EffortRange,
    EventSpec,
    ExperimentDefinition,
    Finding,
    GameDesignResult,
    OutcomeInterpretation,
    ScopeRiskResult,
    TechnicalResult,
)


FEATURE = """
Feature Name:
Repeat Engagement Experiment

Feature Idea:
Create a multi-step engagement feature where players complete
a recurring activity.

If the player does not participate, a future opportunity may become
unavailable.

Player/Product Goal:
Test whether a visible future consequence increases repeat engagement.

Repository Evidence:
No repository evidence is available in this synthetic test.

Studio Decision Thresholds:
No numeric success thresholds have been supplied.
"""
GAME_DESIGN = GameDesignResult(
    agent="game_design",
    recommendation="prototype_first",
    confidence=Confidence.MEDIUM,
    confidence_reason=(
        "Enough information exists to define the minimum player experience, "
        "but reward value and current player behaviour are unknown."
    ),
    assumptions=[
        "Players can understand that missing participation affects future rewards.",
    ],
    findings=[
        Finding(
            statement=(
                "A genuine multi-day experience may be needed to test "
                "daily return behaviour."
            ),
            evidence_ids=[],
        ),
    ],
    risks=[
        "Loss framing may feel punitive.",
        "Weak reward value may create a false negative.",
    ],
    unknowns=[
        "Exact future reward value.",
        "Existing daily-return behaviour.",
    ],
    evidence=[],
    player_problem=(
        "Players may lack a strong reason to return consistently each day."
    ),
    player_value=(
        "Daily participation protects access to future reward opportunities."
    ),
    core_loop=[
        "See daily challenge.",
        "Participate or skip.",
        "Observe effect on future reward availability.",
    ],
    minimum_experience=(
        "A multi-day experience where the participation consequence "
        "is visible to the player."
    ),
    unnecessary_complexity=[
        "Additional systems unrelated to the core hypothesis.",
        "Multiple reward systems.",
    ],
)


TECHNICAL = TechnicalResult(
    agent="technical",
    recommendation="needs_information",
    confidence=Confidence.LOW,
    confidence_reason=(
        "Repository evidence is missing, so reuse and implementation "
        "complexity cannot be verified."
    ),
    assumptions=[
        "The experiment requires participation state across the test period.",
        "The experiment requires a reliable way to determine reward eligibility.",
    ],
    findings=[
        Finding(
            statement=(
                "Repository evidence is required before technical reuse "
                "and effort can be estimated."
            ),
            evidence_ids=[],
        ),
    ],
    risks=[
        "Incorrect participation state could invalidate the experiment.",
    ],
    unknowns=[
        "Existing persistence capability.",
        "Existing reward eligibility capability.",
        "Existing event/configuration capability.",
    ],
    evidence=[],
    systems_affected=[
        "Participation-state capability.",
        "Reward-eligibility capability.",
        "Experiment lifecycle capability.",
    ],
    reuse_points=[],
    new_code_required=[
        "Cannot determine reuse versus new implementation without repository inspection.",
    ],
    service_dependencies=[],
    configuration_changes=[],
    qa_impact=[
        "Participation and eligibility behaviour must be validated.",
    ],
    integration_risks=[
        "Unknown existing architecture may affect experiment feasibility.",
    ],
    effort=EffortRange(
        developer_days_min=None,
        developer_days_max=None,
        basis="No repository evidence is available.",
        confidence=Confidence.LOW,
    ),
)


ANALYTICS = AnalyticsResult(
    agent="analytics",
    recommendation="prototype_first",
    confidence=Confidence.MEDIUM,
    confidence_reason=(
        "The behavioural hypothesis can be tested, but studio decision "
        "thresholds are not defined."
    ),
    assumptions=[
        "Players understand the future-reward consequence.",
    ],
    findings=[
        Finding(
            statement=(
                "A shorter controlled experiment may test the core "
                "behavioural assumption without building all seven days."
            ),
            evidence_ids=[],
        ),
    ],
    risks=[
        "Reward value or messaging could confound behaviour.",
    ],
    unknowns=[
        "Baseline participation.",
        "Studio success threshold.",
    ],
    evidence=[],
    experiment=ExperimentDefinition(
        player_problem=(
            "Players need a stronger reason to return on subsequent days."
        ),
        hypothesis=(
            "If skipping participation affects future reward availability, "
            "players may return and participate more consistently because "
            "they value preserving those future opportunities."
        ),
        riskiest_assumption=(
            "Players understand and value the future reward consequence "
            "enough for it to change behaviour."
        ),
        smallest_experiment=(
            "A 3-day loss-vs-no-loss controlled experiment."
        ),
        build=[
            "Minimal daily challenge.",
            "Visible future-reward consequence.",
            "Conceptual experiment instrumentation.",
        ],
        do_not_build=[
            "Full 7-day content set.",
            "Complex progression systems.",
        ],
        behavioural_question=(
            "Does a future-reward consequence change participation "
            "and subsequent return behaviour?"
        ),
        events=[
            EventSpec(
                event="ChallengeExposure",
                purpose="Observe experiment exposure.",
                properties=["variant", "day_index"],
            ),
            EventSpec(
                event="ChallengeParticipation",
                purpose="Observe participation behaviour.",
                properties=["variant", "day_index"],
            ),
        ],
        funnel=[
            "Challenge exposed",
            "Challenge participated",
            "Player returns",
            "Future reward outcome observed",
        ],
        success_metrics=[
            "Participation by variant.",
            "Subsequent return by variant.",
        ],
        failure_signals=[
            "No interpretable behavioural difference.",
            "Reward consequence is not applied reliably.",
        ],
        observation_window="3 days",
        segments=[
            "Loss variant",
            "No-loss variant",
        ],
        learning_cases=[
            OutcomeInterpretation(
                observed_pattern=(
                    "Loss variant shows stronger subsequent participation."
                ),
                interpretation=(
                    "Supports the hypothesis that future reward availability "
                    "influences behaviour."
                ),
                action="scale",
            ),
            OutcomeInterpretation(
                observed_pattern=(
                    "Experiment implementation or measurement is unreliable."
                ),
                interpretation=(
                    "Behavioural results cannot be interpreted."
                ),
                action="need_more_data",
            ),
        ],
        decision_threshold_notes=[
            "Director/Product decision required",
        ],
    ),
)


SCOPE_RISK = ScopeRiskResult(
    agent="scope_risk",
    recommendation="reduce_scope",
    confidence=Confidence.LOW,
    confidence_reason=(
        "A smaller test appears possible, but repository and economy "
        "context are unavailable."
    ),
    assumptions=[
        "The future consequence must be real and observable.",
    ],
    findings=[
        Finding(
            statement=(
                "A 2-encounter experiment may preserve the core causal test "
                "while removing most of the 7-day implementation."
            ),
            evidence_ids=[],
        ),
    ],
    risks=[
        "Over-reducing scope could make the result unrepresentative.",
    ],
    unknowns=[
        "Exact reward value.",
        "Player trust impact.",
    ],
    evidence=[],
    cheapest_credible_experiment=(
        "A 2-encounter loss-vs-no-loss experiment: one participation "
        "decision followed by one future-reward consequence observation."
    ),
    removable_scope=[
        "Remaining days of the 7-day event.",
        "Additional reward tiers.",
        "Extra narrative progression.",
    ],
    cheaper_test_options=[
        "Use one future reward opportunity instead of a complete reward sequence.",
    ],
    data_invalidation_risks=[
        "Players do not understand the consequence.",
        "Reward values differ between variants.",
    ],
    player_trust_risks=[
        "Loss framing may feel punitive or unfair.",
    ],
    economy_risks=[
        "Different real reward value between variants could confound behaviour.",
    ],
    technical_risks=[
        "Incorrect participation or reward state could invalidate the test.",
    ],
)

def build_producer_input() -> str:
    return f"""
FEATURE
-------
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

PRODUCER TASK
=============
Create one coherent executable experiment from the four structured
specialist results above.

Preserve material disagreements.

Do not invent repository facts, evidence IDs, implementation effort,
or numeric decision thresholds.
"""

async def main():
    agent = create_producer_agent()

    print("Running Producer...\n")

    producer_input = build_producer_input()

    result = await execute_agent(
        agent,
        producer_input,
    )

    output = result.output

    print("=== PRODUCER RESULT ===")
    print(output.model_dump_json(indent=2))

    print("\n=== AGREEMENTS ===")
    for item in output.agreements:
        print(f"- {item}")

    print("\n=== DISAGREEMENTS ===")
    for item in output.disagreements:
        print(f"- {item}")

    print("\n=== PROPOSED EXPERIMENT ===")
    print(output.proposed_experiment.smallest_experiment)

    print("\n=== EFFORT ===")
    print(f"Min days: {output.effort.developer_days_min}")
    print(f"Max days: {output.effort.developer_days_max}")
    print(f"Confidence: {output.effort.confidence.value}")
    print(f"Basis: {output.effort.basis}")

    print("\n=== HUMAN DECISIONS REQUIRED ===")
    for item in output.human_decisions_required:
        print(f"- {item}")

    usage = result.usage

    print("\n=== USAGE ===")
    print(f"Requests: {usage.requests}")
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print(f"Total tokens: {usage.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
