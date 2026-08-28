import os
from collections.abc import Mapping

from agents import RunConfig


ENABLE_TRACING_ENVIRONMENT_VARIABLE = "COUNCIL_ENABLE_TRACING"
INCLUDE_SENSITIVE_TRACE_DATA_ENVIRONMENT_VARIABLE = (
    "COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA"
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def build_agents_run_config(
    environment: Mapping[str, str] | None = None,
) -> RunConfig:
    values = os.environ if environment is None else environment
    tracing_enabled = _is_enabled(
        values.get(ENABLE_TRACING_ENVIRONMENT_VARIABLE, "")
    )
    include_sensitive_data = tracing_enabled and _is_enabled(
        values.get(
            INCLUDE_SENSITIVE_TRACE_DATA_ENVIRONMENT_VARIABLE,
            "",
        )
    )
    return RunConfig(
        tracing_disabled=not tracing_enabled,
        trace_include_sensitive_data=include_sensitive_data,
        workflow_name="Game Feature Council",
    )


def _is_enabled(value: str) -> bool:
    return value.strip().casefold() in _TRUE_VALUES
