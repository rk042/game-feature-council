import asyncio

from agents import Runner

from council.agents import create_technical_agent


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
Find the smallest technically credible implementation capable of
testing whether players care about protecting these future rewards.

Repository Evidence:
No repository files or implementation details have been supplied
to this Technical Specialist smoke test.

Important:
Do not assume any existing:

- event system
- reward system
- economy system
- analytics implementation
- configuration system
- Unity Gaming Services integration
- Remote Config setup
- Cloud Code module
- persistence architecture
- progression system

If implementation effort cannot be credibly estimated without repository
evidence, return null for the minimum and maximum developer days and
explain why in the effort basis.
"""


async def main():
    agent = create_technical_agent()

    print("Running Technical Specialist...\n")

    result = await Runner.run(
        agent,
        FEATURE,
    )

    output = result.final_output

    print("=== TECHNICAL RESULT ===")
    print(output.model_dump_json(indent=2))

    print("\n=== EFFORT ONLY ===")
    print(f"Min days: {output.effort.developer_days_min}")
    print(f"Max days: {output.effort.developer_days_max}")
    print(f"Confidence: {output.effort.confidence.value}")
    print(f"Basis: {output.effort.basis}")

    usage = result.context_wrapper.usage

    print("\n=== USAGE ===")
    print(f"Requests: {usage.requests}")
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print(f"Total tokens: {usage.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
