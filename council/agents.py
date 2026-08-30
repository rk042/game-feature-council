import os
from pathlib import Path

from agents import Agent

from council.models import (
    AnalyticsResult,
    DirectorResult,
    EvidenceResolverResult,
    FeatureRefinerResult,
    GameDesignResult,
    ProducerResult,
    ScopeRiskResult,
    TechnicalResult,
)


PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


def create_game_design_agent() -> Agent:
    model = os.environ["COUNCIL_SPECIALIST_MODEL"]

    return Agent(
        name="Game Design Specialist",
        instructions=load_prompt("game_design.md"),
        model=model,
        output_type=GameDesignResult,
    )


def create_technical_agent() -> Agent:
    model = os.environ["COUNCIL_SPECIALIST_MODEL"]

    return Agent(
        name="Technical Specialist",
        instructions=load_prompt("technical.md"),
        model=model,
        output_type=TechnicalResult,
    )

def create_analytics_agent() -> Agent:
    model = os.environ["COUNCIL_SPECIALIST_MODEL"]

    return Agent(
        name="Analytics / Experiment Specialist",
        instructions=load_prompt("analytics.md"),
        model=model,
        output_type=AnalyticsResult,
    )

def create_scope_risk_agent() -> Agent:
    model = os.environ["COUNCIL_SPECIALIST_MODEL"]

    return Agent(
        name="Scope / Risk Specialist",
        instructions=load_prompt("scope_risk.md"),
        model=model,
        output_type=ScopeRiskResult,
    )

def create_producer_agent() -> Agent:
    model = os.environ["COUNCIL_SYNTHESIS_MODEL"]

    return Agent(
        name="Producer",
        instructions=load_prompt("producer.md"),
        model=model,
        output_type=ProducerResult,
    )

def create_director_agent() -> Agent:
    model = os.environ["COUNCIL_SYNTHESIS_MODEL"]

    return Agent(
        name="Game Director",
        instructions=load_prompt("director.md"),
        model=model,
        output_type=DirectorResult,
    )


def create_evidence_resolver_agent() -> Agent:
    """Create the Council-only evidence reconciliation stage."""
    model = os.environ.get("COUNCIL_RESOLVER_MODEL", "").strip()
    if not model:
        model = os.environ["COUNCIL_SYNTHESIS_MODEL"]
    return Agent(
        name="Evidence Resolver",
        instructions=load_prompt("evidence_resolver.md"),
        model=model,
        output_type=EvidenceResolverResult,
    )


def create_generalist_agent() -> Agent:
    model = os.environ["COUNCIL_SYNTHESIS_MODEL"]

    return Agent(
        name="Generalist Baseline",
        instructions=load_prompt("generalist.md"),
        model=model,
        output_type=DirectorResult,
    )


def create_feature_refiner_agent(model: str | None = None) -> Agent:
    from council.refinement import configured_refiner_model

    return Agent(
        name="Feature Refiner",
        instructions=load_prompt("feature_refiner.md"),
        model=model or configured_refiner_model(),
        output_type=FeatureRefinerResult,
    )
