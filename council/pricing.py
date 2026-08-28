from datetime import date
from decimal import Decimal
from typing import Mapping

from pydantic import BaseModel, Field

from council.models import RoleTelemetry


TOKENS_PER_MILLION = Decimal("1000000")


class ModelPrice(BaseModel):
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)


class PricingSnapshot(BaseModel):
    identifier: str
    effective_date: date
    models: dict[str, ModelPrice]


class CostEstimate(BaseModel):
    estimated_cost_usd: Decimal | None
    unpriced_models: list[str]


DEFAULT_PRICING_SNAPSHOT = PricingSnapshot(
    identifier="openai-api-pricing-2026-08-28",
    effective_date=date(2026, 8, 28),
    models={
        "gpt-5.4-nano": ModelPrice(
            input_usd_per_million=Decimal("0.20"),
            output_usd_per_million=Decimal("1.25"),
        )
    },
)


def estimate_cost(
    roles: Mapping[str, RoleTelemetry],
    pricing: PricingSnapshot = DEFAULT_PRICING_SNAPSHOT,
) -> CostEstimate:
    total_cost = Decimal("0")
    unpriced_models: set[str] = set()

    for telemetry in roles.values():
        model_price = pricing.models.get(telemetry.model)
        if model_price is None:
            unpriced_models.add(telemetry.model)
            continue

        total_cost += (
            Decimal(telemetry.usage.input_tokens)
            / TOKENS_PER_MILLION
            * model_price.input_usd_per_million
        )
        total_cost += (
            Decimal(telemetry.usage.output_tokens)
            / TOKENS_PER_MILLION
            * model_price.output_usd_per_million
        )

    ordered_unpriced_models = sorted(unpriced_models)
    return CostEstimate(
        estimated_cost_usd=(None if ordered_unpriced_models else total_cost),
        unpriced_models=ordered_unpriced_models,
    )
