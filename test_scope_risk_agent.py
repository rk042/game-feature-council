import asyncio

from council.agents import create_scope_risk_agent
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
Find the smallest credible experiment capable of telling us whether
players care about protecting these future reward opportunities.

Known Specialist Thinking:

Game Design:
A real multi-day experience may be necessary for players to understand
and care about the future-reward consequence.

Analytics / Experiment:
A 3-day micro-experiment with a loss variant and a no-loss control
may be sufficient to test the core behavioural assumption.

Repository Evidence:
No repository files, implementation details, economy architecture,
or project documentation have been supplied.

Studio Decision Thresholds:
No numeric success thresholds have been supplied.

Important:

Challenge the proposed scope.

Do not automatically choose either the Game Design or Analytics proposal.

Determine whether the same learning could be obtained more cheaply
or safely without invalidating the experiment.

Do not invent repository architecture, implementation effort,
analytics thresholds, or economy systems.
"""


async def main():
    agent = create_scope_risk_agent()

    print("Running Scope / Risk Specialist...\n")

    result = await execute_agent(
        agent,
        FEATURE,
    )

    output = result.output

    print("=== SCOPE / RISK RESULT ===")
    print(output.model_dump_json(indent=2))

    print("\n=== SCOPE SUMMARY ===")
    print(
        f"Cheapest credible experiment: "
        f"{output.cheapest_credible_experiment}"
    )

    print("\n=== REMOVABLE SCOPE ===")
    for item in output.removable_scope:
        print(f"- {item}")

    print("\n=== CHEAPER TEST OPTIONS ===")
    for item in output.cheaper_test_options:
        print(f"- {item}")

    usage = result.usage

    print("\n=== USAGE ===")
    print(f"Requests: {usage.requests}")
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print(f"Total tokens: {usage.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
