import asyncio

from council.agents import create_analytics_agent
from council.execution import execute_agent


FEATURE = """
Feature Name:
Repeat Engagement Experiment

Feature Idea:
Create a 7-day event where players complete one daily challenge
to preserve access to a future opportunity.

If the player does not participate, some future reward opportunities
become unavailable.

Player/Product Goal:
Give players a stronger reason to return each day.

Prototype Constraint:
Find the smallest possible experiment capable of telling us whether
players care about protecting these future reward opportunities.

Repository Evidence:
No repository files, analytics implementation details, existing event
names, analytics SDK information, or project documentation have been
supplied to this smoke test.

Studio Decision Thresholds:
No numeric success thresholds have been supplied.

Important:

Do not invent:

- existing analytics events
- analytics SDKs
- retention targets
- conversion targets
- acceptable participation percentages
- statistical significance requirements
- sample sizes
- repository implementation details

When a decision requires a studio-defined numeric threshold, state:

Director/Product decision required
"""


async def main():
    agent = create_analytics_agent()

    print("Running Analytics / Experiment Specialist...\n")

    result = await execute_agent(
        agent,
        FEATURE,
    )

    output = result.output

    print("=== ANALYTICS RESULT ===")
    print(output.model_dump_json(indent=2))

    print("\n=== EXPERIMENT SUMMARY ===")
    print(f"Problem: {output.experiment.player_problem}")
    print(f"Hypothesis: {output.experiment.hypothesis}")
    print(
        f"Riskiest assumption: "
        f"{output.experiment.riskiest_assumption}"
    )
    print(
        f"Smallest experiment: "
        f"{output.experiment.smallest_experiment}"
    )

    print("\n=== DECISION THRESHOLD NOTES ===")
    for note in output.experiment.decision_threshold_notes:
        print(f"- {note}")

    usage = result.usage

    print("\n=== USAGE ===")
    print(f"Requests: {usage.requests}")
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print(f"Total tokens: {usage.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
