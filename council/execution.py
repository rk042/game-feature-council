from dataclasses import dataclass

from agents import Agent, Runner
from pydantic import BaseModel

from council.models import TokenUsage
from council.tracing import build_agents_run_config


@dataclass(frozen=True)
class AgentCallResult:
    output: BaseModel
    usage: TokenUsage


async def execute_agent(agent: Agent, input_text: str) -> AgentCallResult:
    result = await Runner.run(
        agent,
        input_text,
        run_config=build_agents_run_config(),
    )
    sdk_usage = result.context_wrapper.usage
    return AgentCallResult(
        output=result.final_output,
        usage=TokenUsage(
            requests=sdk_usage.requests,
            input_tokens=sdk_usage.input_tokens,
            output_tokens=sdk_usage.output_tokens,
            total_tokens=sdk_usage.total_tokens,
        ),
    )
