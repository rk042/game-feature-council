import asyncio

from agents import Runner

from council.agents import create_game_design_agent


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
Find the smallest possible experience capable of telling us whether
players actually care about protecting these future rewards.
"""


async def main():
    agent = create_game_design_agent()

    print("Running Game Design Specialist...\n")

    result = await Runner.run(
        agent,
        FEATURE,
    )

    output = result.final_output

    print("=== STRUCTURED RESULT ===")
    print(output.model_dump_json(indent=2))

    usage = result.context_wrapper.usage

    print("\n=== USAGE ===")
    print(f"Requests: {usage.requests}")
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print(f"Total tokens: {usage.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
