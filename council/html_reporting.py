from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from decimal import Decimal
from html import escape

from council.models import (
    ComparisonRecord,
    ContextBundle,
    DirectorResult,
    FeatureRefinementRecord,
    GeneralistExecution,
    RoleTelemetry,
    RunRecord,
    SpecialistCommon,
)
from council.pricing import DEFAULT_PRICING_SNAPSHOT, estimate_cost


ROLE_ORDER = (
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
    "producer",
    "director",
)
ROLE_LABELS = {
    "game_design": "Game Design",
    "technical": "Technical",
    "analytics": "Analytics",
    "scope_risk": "Scope / Risk",
    "producer": "Producer",
    "director": "Director",
    "generalist": "Generalist",
}
ROLE_ACCENTS = {
    "game_design": "violet",
    "technical": "blue",
    "analytics": "cyan",
    "scope_risk": "amber",
    "producer": "pink",
    "director": "green",
    "generalist": "slate",
}
RUBRIC_LABELS = (
    ("grounding", "Grounding"),
    ("scope_reduction", "Scope reduction"),
    ("hypothesis_quality", "Hypothesis quality"),
    ("experiment_credibility", "Experiment credibility"),
    ("measurement_to_learning_logic", "Measurement-to-learning logic"),
    ("technical_realism", "Technical realism"),
    ("decision_usefulness", "Decision usefulness"),
    ("conciseness", "Conciseness"),
)


def render_html_report(
    record: RunRecord,
    comparison: ComparisonRecord | None = None,
    *,
    risks_and_unknowns: Iterable[str] = (),
    feature_refinement: FeatureRefinementRecord | None = None,
) -> str:
    result = record.council_result
    director = result.director
    mode = "Council + Generalist" if comparison is not None else "Council"
    roles = dict(record.telemetry.roles)
    if comparison is not None:
        roles["generalist"] = comparison.generalist.telemetry

    sections = [
        ("executive", "Executive Summary"),
        ("decision", "Council Decision"),
        ("workflow", "Experiment Workflow"),
        ("specialists", "Specialist Findings"),
        ("producer", "Producer Synthesis"),
    ]
    if feature_refinement is not None:
        sections.insert(1, ("feature-refinement", "Feature Refinement"))
    if comparison is not None:
        sections.append(("comparison", "Generalist Comparison"))
    sections.extend(
        [
            ("metrics", "Metrics & Charts"),
            ("risks", "Risks & Unknowns"),
            ("evidence", "Repository Evidence"),
            ("human-review", "Human Review"),
        ]
    )

    body = "".join(
        [
            _header(
                run_id=record.run_id,
                started_at=record.telemetry.started_at.isoformat(),
                mode=mode,
            ),
            _ai_disclaimer(),
            _council_executive(record, comparison, feature_refinement),
            _feature_repository(record.feature_input, result.context),
            _feature_refinement_section(feature_refinement),
            _director_section(
                director,
                record.telemetry.roles.get("director"),
                questions_resolved=record.human_decision is not None,
            ),
            _workflow_section(
                director,
                include_generalist=comparison is not None,
            ),
            _specialists_section(record),
            _producer_section(record),
            (
                _comparison_section(record, comparison)
                if comparison is not None
                else ""
            ),
            _metrics_section(
                roles,
                pricing_snapshot_ids={
                    **{
                        role: record.pricing_snapshot_id
                        for role in record.telemetry.roles
                    },
                    **(
                        {
                            "generalist": comparison.generalist.pricing_snapshot_id
                        }
                        if comparison is not None
                        else {}
                    ),
                },
            ),
            _risks_section(risks_and_unknowns),
            _evidence_section(result.context, _specialist_mapping(record)),
            _human_review_section(record, comparison),
            _footer(record.pricing_snapshot_id),
        ]
    )
    return _document(record.run_id, sections, body)


def render_generalist_html_report(
    run_id: str,
    feature_input: str,
    context: ContextBundle,
    execution: GeneralistExecution,
    *,
    started_at: str,
    feature_refinement: FeatureRefinementRecord | None = None,
) -> str:
    result = execution.result
    sections = [
        ("executive", "Executive Summary"),
        ("recommendation", "Generalist Recommendation"),
        ("workflow", "Baseline Workflow"),
        ("metrics", "Metrics & Charts"),
        ("risks", "Unknowns"),
        ("evidence", "Repository Evidence"),
        ("human-review", "Human Review"),
    ]
    if feature_refinement is not None:
        sections.insert(1, ("feature-refinement", "Feature Refinement"))
    body = "".join(
        [
            _header(run_id=run_id, started_at=started_at, mode="Generalist"),
            _ai_disclaimer(),
            _generalist_executive(execution, feature_refinement),
            _feature_repository(feature_input, context),
            _feature_refinement_section(feature_refinement),
            _generalist_recommendation(result),
            _generalist_workflow(),
            _metrics_section(
                {"generalist": execution.telemetry},
                pricing_snapshot_ids={
                    "generalist": execution.pricing_snapshot_id
                },
            ),
            _risks_section(result.unresolved_unknowns, title="Unknowns"),
            _evidence_section(context, {}),
            _generalist_human_review(),
            _footer(execution.pricing_snapshot_id),
        ]
    )
    return _document(run_id, sections, body)


def _document(
    run_id: str,
    sections: list[tuple[str, str]],
    body: str,
) -> str:
    navigation = "".join(
        f'<a href="#{section_id}">{_h(label)}</a>'
        for section_id, label in sections
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Game Feature Council - {_h(run_id)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar" aria-label="Report navigation">
      <div class="brand-mark" aria-hidden="true">GFC</div>
      <nav>{navigation}</nav>
    </aside>
    <main>{body}</main>
  </div>
</body>
</html>
"""


def _header(*, run_id: str, started_at: str, mode: str) -> str:
    return f"""
<header class="hero">
  <p class="eyebrow">Repository-grounded feature evaluation</p>
  <h1>Game Feature Council</h1>
  <div class="run-meta">
    <span><b>Run ID</b> {_h(run_id)}</span>
    <span><b>Started</b> {_h(started_at)}</span>
    <span><b>Mode</b> {_h(mode)}</span>
  </div>
</header>
"""


def _ai_disclaimer() -> str:
    return """
<aside class="ai-disclaimer" aria-label="AI-generated analysis warning">
  <strong>AI-Generated Analysis</strong>
  <p>This report contains findings generated with AI. AI systems can make mistakes
  or misinterpret repository context.</p>
  <p>Review the repository evidence, source documents, specialist findings,
  and recommendations manually before making implementation, product,
  economy, or production decisions.</p>
</aside>
"""


def _council_executive(
    record: RunRecord,
    comparison: ComparisonRecord | None,
    refinement: FeatureRefinementRecord | None,
) -> str:
    director = record.council_result.director
    usage = record.telemetry.total_usage
    calls = usage.requests
    tokens = usage.total_tokens
    cost: Decimal | None = record.estimated_cost_usd
    cost_label = _persisted_cost(cost, record.unpriced_models)
    metrics = [
        ("Director Decision", director.decision.value),
        ("Confidence", director.confidence.value),
        ("Council Cost", cost_label),
        ("Council Tokens", f"{tokens:,}"),
        ("Council Duration", _duration(record.telemetry.total_duration_ms)),
        ("Council Calls", str(calls)),
    ]
    if comparison is not None:
        generalist = comparison.generalist_metrics
        combined_cost = (
            record.estimated_cost_usd + generalist.estimated_cost_usd
            if record.estimated_cost_usd is not None
            and generalist.estimated_cost_usd is not None
            else None
        )
        metrics.extend(
            [
                (
                    (
                        "Known Downstream Cost"
                        if refinement is not None
                        and not refinement.usage_complete
                        else "Combined Cost"
                    ),
                    _persisted_cost(
                        combined_cost,
                        (
                            *record.unpriced_models,
                            *comparison.generalist.unpriced_models,
                        ),
                    ),
                ),
                (
                    "Combined Tokens",
                    f"{tokens + generalist.total_tokens:,}",
                ),
                (
                    "Combined Calls",
                    str(calls + comparison.generalist.telemetry.usage.requests),
                ),
                ("Generalist Decision", generalist.decision.value),
                ("Generalist Confidence", generalist.confidence.value),
                (
                    "Generalist Cost",
                    _persisted_cost(
                        generalist.estimated_cost_usd,
                        comparison.generalist.unpriced_models,
                    ),
                ),
                ("Generalist Tokens", f"{generalist.total_tokens:,}"),
            ]
        )
    if refinement is not None:
        downstream_costs = [record.estimated_cost_usd]
        downstream_tokens = tokens
        downstream_calls = calls
        unpriced = [*record.unpriced_models, *refinement.unpriced_models]
        if comparison is not None:
            downstream_costs.append(
                comparison.generalist.estimated_cost_usd
            )
            downstream_tokens += comparison.generalist.telemetry.usage.total_tokens
            downstream_calls += comparison.generalist.telemetry.usage.requests
            unpriced.extend(comparison.generalist.unpriced_models)
        all_costs = [refinement.estimated_cost_usd, *downstream_costs]
        known_cost = (
            _sum_complete_decimals(all_costs)
            if refinement.usage_complete
            else _sum_known_decimals(all_costs)
        )
        metrics.extend(
            [
                (
                    "Known Refiner Cost",
                    _refinement_persisted_cost(refinement),
                ),
                ("Refiner Tokens", f"{refinement.telemetry.usage.total_tokens:,}"),
                ("Refiner Attempts", str(refinement.attempted_calls)),
                ("Refiner Completed Calls", str(refinement.refinement_rounds)),
                (
                    "Overall Tokens",
                    f"{downstream_tokens + refinement.telemetry.usage.total_tokens:,}",
                ),
            ]
        )
        if refinement.usage_complete:
            metrics.extend(
                [
                    ("Overall Cost", _persisted_cost(known_cost, unpriced)),
                    (
                        "Overall Calls",
                        str(downstream_calls + refinement.refinement_rounds),
                    ),
                ]
            )
        else:
            metrics.extend(
                [
                    ("Known Session Cost", _persisted_cost(known_cost, unpriced)),
                    ("Session Cost", "Incomplete"),
                    ("Usage", "Incomplete"),
                ]
            )
    return _metric_section(metrics)


def _generalist_executive(
    execution: GeneralistExecution,
    refinement: FeatureRefinementRecord | None,
) -> str:
    result = execution.result
    usage = execution.telemetry.usage
    metrics = [
            ("Generalist Decision", result.decision.value),
            ("Confidence", result.confidence.value),
            (
                "Estimated Cost",
                _persisted_cost(
                    execution.estimated_cost_usd,
                    execution.unpriced_models,
                ),
            ),
            ("Total Tokens", f"{usage.total_tokens:,}"),
            ("Duration", _duration(execution.telemetry.duration_ms)),
            ("Model Calls", str(usage.requests)),
        ]
    if refinement is not None:
        costs = [refinement.estimated_cost_usd, execution.estimated_cost_usd]
        known_cost = (
            _sum_complete_decimals(costs)
            if refinement.usage_complete
            else _sum_known_decimals(costs)
        )
        metrics.extend(
            [
                (
                    "Known Refiner Cost",
                    _refinement_persisted_cost(refinement),
                ),
                ("Refiner Tokens", f"{refinement.telemetry.usage.total_tokens:,}"),
                ("Refiner Attempts", str(refinement.attempted_calls)),
                ("Refiner Completed Calls", str(refinement.refinement_rounds)),
                (
                    "Overall Tokens",
                    f"{usage.total_tokens + refinement.telemetry.usage.total_tokens:,}",
                ),
            ]
        )
        all_unpriced = (
            *refinement.unpriced_models,
            *execution.unpriced_models,
        )
        if refinement.usage_complete:
            metrics.extend(
                [
                    ("Overall Cost", _persisted_cost(known_cost, all_unpriced)),
                    (
                        "Overall Calls",
                        str(usage.requests + refinement.refinement_rounds),
                    ),
                ]
            )
        else:
            metrics.extend(
                [
                    (
                        "Known Session Cost",
                        _persisted_cost(known_cost, all_unpriced),
                    ),
                    ("Session Cost", "Incomplete"),
                    ("Usage", "Incomplete"),
                ]
            )
    return _metric_section(metrics)


def _metric_section(metrics: Iterable[tuple[str, str]]) -> str:
    cards = "".join(
        f'<article class="metric"><span>{_h(label)}</span><strong>{_h(value)}</strong></article>'
        for label, value in metrics
        if value
    )
    return f'<section id="executive"><h2>Executive Summary</h2><div class="metric-grid">{cards}</div></section>'


def _feature_repository(feature: str, context: ContextBundle) -> str:
    evidence_count = len(context.evidence)
    return f"""
<section class="context-strip" aria-label="Feature and repository identity">
  <div>
    <h2>Feature Input</h2>
    <p class="prewrap">{_h(feature)}</p>
  </div>
  <div>
    <h2>Repository</h2>
    <dl class="facts">
      <dt>Path</dt><dd>{_h(context.repository_path)}</dd>
      <dt>Branch</dt><dd>{_h(context.branch or 'detached HEAD')}</dd>
      <dt>Commit</dt><dd><code>{_h(context.commit_sha)}</code></dd>
      <dt>Tracked tree dirty</dt><dd>{_h(str(context.working_tree_dirty))}</dd>
      <dt>Selected evidence</dt><dd>{evidence_count}</dd>
    </dl>
  </div>
</section>
"""


def _feature_refinement_section(
    refinement: FeatureRefinementRecord | None,
) -> str:
    if refinement is None:
        return ""
    if refinement.approved:
        status = "User approved; refined brief evaluated downstream"
        brief_heading = "Approved Refined Feature Brief"
        brief = refinement.approved_refined_feature or ""
    else:
        status = "Not approved; original request evaluated downstream"
        if refinement.rounds:
            brief_heading = "Latest AI Proposal (not evaluated)"
            brief = refinement.rounds[-1].result.refined_brief
        else:
            brief_heading = "AI Interpretation"
            brief = "No interpretation completed because the attempt failed."
    unresolved = (
        "<div class=\"card\"><h3>Unresolved Before Repository Analysis</h3>"
        + _text_list(refinement.unresolved_points)
        + "</div>"
        if refinement.unresolved_points
        else ""
    )
    usage_note = (
        ""
        if refinement.usage_complete
        else (
            "<p><b>Feature Refiner usage: Incomplete.</b> "
            "Known costs exclude one failed attempt because it did not return "
            "usage telemetry. Reason: "
            f"{_h(refinement.usage_unavailable_reason or 'Usage unavailable.')}"
            "</p>"
        )
    )
    cost_label = (
        "Known Refiner cost"
        if not refinement.usage_complete
        else "Refiner cost"
    )
    return f"""
<section id="feature-refinement">
  <h2>Feature Refinement</h2>
  <p class="section-note">AI-generated interpretation preserved for auditability. The user approval status below determines the canonical feature input.</p>
  <div class="two-column">
    <div class="card"><h3>Original Feature Request</h3><p class="prewrap">{_h(refinement.original_feature)}</p></div>
    <div class="card"><h3>Status</h3><p>{_h(status)}</p><p><b>Attempts:</b> {refinement.attempted_calls}</p><p><b>Completed rounds:</b> {refinement.refinement_rounds}</p></div>
  </div>
  <div class="three-column">
    <div class="card"><h3>Concise Interpretation</h3>{_text_list(refinement.concise_interpretation) if refinement.concise_interpretation else '<p>Not available because the attempt failed.</p>'}</div>
    {unresolved}
    <div class="card"><h3>Preserved Constraints</h3>{_text_list(refinement.preserved_constraints)}</div>
  </div>
  <details><summary>{_h(brief_heading)}</summary><p class="prewrap">{_h(brief)}</p></details>
  <p><b>Shared preprocessing telemetry:</b> model {_h(refinement.telemetry.model)}; {_h(str(refinement.telemetry.duration_ms))} ms; {refinement.telemetry.usage.total_tokens} known tokens; {refinement.refinement_rounds} completed calls; {cost_label} {_refinement_persisted_cost(refinement)}; pricing snapshot {_h(refinement.pricing_snapshot_id)}.</p>
  {usage_note}
</section>
"""


def _director_section(
    director: DirectorResult,
    telemetry: RoleTelemetry | None,
    *,
    questions_resolved: bool,
) -> str:
    rationale = _text_list(director.rationale)
    question_values = director.human_decisions_required
    if questions_resolved:
        question_values = [
            f"Resolved: {question}"
            for question in question_values
        ]
    questions = _text_list(question_values)
    questions_heading = (
        "Director Questions - Resolved by Human Decision"
        if questions_resolved
        else "Human Decisions Required"
    )
    conditions = director.decision_conditions
    return f"""
<section id="decision" class="decision-panel">
  <div class="section-heading">{_role_icon("director")}<div><div class="decision-kicker">Director recommendation</div></div></div>
  <div class="decision-value">{_h(director.decision.value)}</div>
  <div class="badge">Confidence: {_h(director.confidence.value)}</div>
  <p>{_h(director.confidence_reason)}</p>
  <p><b>Effort:</b> {_h(_effort(director.effort.developer_days_min, director.effort.developer_days_max))} | <b>Effort confidence:</b> {_h(director.effort.confidence.value)} | {_h(director.effort.basis)}</p>
  {_role_telemetry(telemetry)}
  <div class="two-column">
    <div><h3>Rationale</h3>{rationale}</div>
    <div><h3>{questions_heading}</h3>{questions}</div>
  </div>
  <div class="condition-grid">
    {_condition("Scale", conditions.scale)}
    {_condition("Iterate", conditions.iterate)}
    {_condition("Kill", conditions.kill)}
    {_condition("Need More Data", conditions.need_more_data)}
  </div>
</section>
"""


def _workflow_section(
    director: DirectorResult,
    *,
    include_generalist: bool,
) -> str:
    experiment = director.experiment
    measure = [
        experiment.behavioural_question,
        *experiment.success_metrics,
        *(
            f"Event: {event.event} - {event.purpose}"
            for event in experiment.events
        ),
    ]
    learn = [
        f"{case.observed_pattern} - {case.interpretation} -> {case.action}"
        for case in experiment.learning_cases
    ]
    decide = [
        *(f"Scale: {value}" for value in director.decision_conditions.scale),
        *(f"Iterate: {value}" for value in director.decision_conditions.iterate),
        *(f"Kill: {value}" for value in director.decision_conditions.kill),
        *(
            f"Need More Data: {value}"
            for value in director.decision_conditions.need_more_data
        ),
    ]
    return f"""
<section id="workflow">
  <h2>Build - Measure - Learn - Decide</h2>
  <p class="section-note">Direct rendering of persisted experiment fields.</p>
  <div class="flow-grid">
    {_flow_step("Build", experiment.build)}
    {_flow_step("Measure", measure)}
    {_flow_step("Learn", learn)}
    {_flow_step("Decide", decide)}
  </div>
  {_architecture_diagram(include_generalist=include_generalist)}
</section>
"""


def _generalist_workflow() -> str:
    return f"""
<section id="workflow">
  <h2>Baseline Workflow</h2>
  <p class="section-note">Structural explanation; not an additional finding.</p>
  {_architecture_diagram(include_generalist=True, generalist_only=True)}
</section>
"""


def _architecture_diagram(
    *,
    include_generalist: bool,
    generalist_only: bool = False,
) -> str:
    if generalist_only:
        council = ""
    else:
        council = """
<div class="architecture-lane">
  <div class="arch-group"><b>Specialists</b><span>Game Design</span><span>Technical</span><span>Analytics</span><span>Scope / Risk</span></div>
  <span class="arrow" aria-hidden="true">&rarr;</span>
  <div class="arch-node">Producer</div>
  <span class="arrow" aria-hidden="true">&rarr;</span>
  <div class="arch-node">Director</div>
</div>"""
    baseline = ""
    if generalist_only:
        baseline = """
<div class="architecture-lane baseline">
  <div class="arch-node">Feature + Context</div>
  <span class="arrow" aria-hidden="true">&rarr;</span>
  <div class="arch-node">Generalist</div>
  <span class="arrow" aria-hidden="true">&rarr;</span>
  <div class="arch-node">Baseline Result</div>
</div>"""
    elif include_generalist:
        baseline = """
<div class="architecture-lane baseline">
  <div class="arch-node">Same Feature + Same Context</div>
  <span class="arrow" aria-hidden="true">&rarr;</span>
  <div class="arch-node">Generalist</div>
  <span class="arrow" aria-hidden="true">&rarr;</span>
  <div class="arch-node">Human Comparison</div>
</div>"""
    return f'<div class="architecture" aria-label="Evaluation architecture">{council}{baseline}</div>'


def _specialists_section(record: RunRecord) -> str:
    specialists = _specialist_mapping(record)
    cards = "".join(
        _specialist_card(
            role,
            specialist,
            record.telemetry.roles.get(role),
        )
        for role, specialist in specialists.items()
    )
    return f'<section id="specialists"><h2>Specialist Findings</h2><div class="role-grid">{cards}</div></section>'


def _specialist_card(
    role: str,
    specialist: SpecialistCommon,
    telemetry: RoleTelemetry | None,
) -> str:
    telemetry_html = _role_telemetry(telemetry)
    findings = "".join(
        f'<li><span>{_h(finding.statement)}</span>{_evidence_chips(finding.evidence_ids)}</li>'
        for finding in specialist.findings
    ) or "<li>None</li>"
    return f"""
<article class="role-card {ROLE_ACCENTS[role]}">
  <header>{_role_icon(role)}<div><h3>{_h(ROLE_LABELS[role])}</h3><span>{_h(specialist.recommendation)}</span></div></header>
  <p><b>Confidence:</b> {_h(specialist.confidence.value)}</p>
  <p>{_h(specialist.confidence_reason)}</p>
  {telemetry_html}
  <h4>Findings</h4><ul class="finding-list">{findings}</ul>
</article>
"""


def _producer_section(record: RunRecord) -> str:
    producer = record.council_result.producer
    telemetry = record.telemetry.roles.get("producer")
    return f"""
<section id="producer">
  <div class="section-heading">{_role_icon("producer")}<div><h2>Producer Synthesis</h2><p>Structured synthesis of specialist outputs; not a debate transcript.</p></div></div>
  {_role_telemetry(telemetry)}
  <div class="three-column">
    <div class="card"><h3>Agreements</h3>{_text_list(producer.agreements)}</div>
    <div class="card"><h3>Disagreements</h3>{_text_list(producer.disagreements)}</div>
    <div class="card"><h3>Dependencies</h3>{_text_list(producer.dependencies)}</div>
  </div>
  <div class="card"><h3>Execution Steps</h3>{_numbered_list(producer.execution_steps)}</div>
</section>
"""


def _generalist_recommendation(result: DirectorResult) -> str:
    return f"""
<section id="recommendation" class="decision-panel generalist">
  <div class="decision-kicker">Generalist baseline recommendation</div>
  <div class="decision-value">{_h(result.decision.value)}</div>
  <div class="badge">Confidence: {_h(result.confidence.value)}</div>
  <p>{_h(result.confidence_reason)}</p>
  <h3>Rationale</h3>{_text_list(result.rationale)}
</section>
"""


def _comparison_section(
    record: RunRecord,
    comparison: ComparisonRecord,
) -> str:
    council = comparison.council_metrics
    generalist = comparison.generalist_metrics
    review = comparison.human_review
    radar = _radar_chart(review) if review is not None else ""
    if review is None:
        human = f"""
<div class="pending-card">
  <strong>Human comparison pending</strong>
  <p>Complete it with <code>council review --run {_h(record.run_id)}</code>.</p>
</div>"""
    else:
        human = f"""
<div class="card">
  <h3>Human Evaluation</h3>
  <p><b>Preference:</b> {_h(review.preference.value)}</p>
  <p><b>Reason:</b> {_h(review.reason)}</p>
</div>{radar}"""
    return f"""
<section id="comparison">
  <h2>Council vs Generalist</h2>
  <div class="comparison-grid">
    {_comparison_path("Council", council)}
    {_comparison_path("Generalist", generalist)}
  </div>
  {human}
</section>
"""


def _comparison_path(label: str, metrics) -> str:
    return f"""
<article class="card">
  <h3>{_h(label)}</h3>
  <dl class="facts">
    <dt>Decision</dt><dd>{_h(metrics.decision.value)}</dd>
    <dt>Confidence</dt><dd>{_h(metrics.confidence.value)}</dd>
    <dt>Duration</dt><dd>{_duration(metrics.duration_ms)}</dd>
    <dt>Input tokens</dt><dd>{metrics.input_tokens:,}</dd>
    <dt>Output tokens</dt><dd>{metrics.output_tokens:,}</dd>
    <dt>Total tokens</dt><dd>{metrics.total_tokens:,}</dd>
    <dt>Estimated cost</dt><dd>{_persisted_cost(metrics.estimated_cost_usd, ())}</dd>
  </dl>
</article>
"""


def _radar_chart(review) -> str:
    generalist_scores = [
        getattr(review.generalist_scores, field) for field, _ in RUBRIC_LABELS
    ]
    council_scores = [
        getattr(review.council_scores, field) for field, _ in RUBRIC_LABELS
    ]
    center = 170.0
    radius = 112.0

    def points(scores: Iterable[int]) -> str:
        return " ".join(
            f"{x:.1f},{y:.1f}"
            for x, y in _radar_points(list(scores), center, radius)
        )

    grids = "".join(
        f'<polygon class="radar-grid" points="{points([level] * 8)}"></polygon>'
        for level in range(1, 6)
    )
    axes = "".join(
        f'<line class="radar-axis" x1="{center}" y1="{center}" x2="{x:.1f}" y2="{y:.1f}"></line>'
        for x, y in _radar_points([5] * 8, center, radius)
    )
    rows = "".join(
        f"<tr><th>{_h(label)}</th><td>{generalist}</td><td>{council}</td></tr>"
        for (_, label), generalist, council in zip(
            RUBRIC_LABELS,
            generalist_scores,
            council_scores,
            strict=True,
        )
    )
    return f"""
<div id="comparison-radar" class="radar-layout">
  <svg class="radar" viewBox="0 0 340 340" role="img" aria-label="Human Council versus Generalist rubric scores from one to five">
    {grids}{axes}
    <polygon class="radar-series generalist-series" points="{points(generalist_scores)}"></polygon>
    <polygon class="radar-series council-series" points="{points(council_scores)}"></polygon>
  </svg>
  <div>
    <div class="legend"><span class="generalist-key">Generalist</span><span class="council-key">Council</span></div>
    <table><thead><tr><th>Dimension</th><th>Generalist</th><th>Council</th></tr></thead><tbody>{rows}</tbody></table>
  </div>
</div>
"""


def _radar_points(
    scores: list[int],
    center: float,
    radius: float,
) -> list[tuple[float, float]]:
    return [
        (
            center
            + math.cos(-math.pi / 2 + index * 2 * math.pi / len(scores))
            * radius
            * score
            / 5,
            center
            + math.sin(-math.pi / 2 + index * 2 * math.pi / len(scores))
            * radius
            * score
            / 5,
        )
        for index, score in enumerate(scores)
    ]


def _metrics_section(
    roles: Mapping[str, RoleTelemetry],
    *,
    pricing_snapshot_ids: Mapping[str, str],
) -> str:
    ordered_roles = [role for role in (*ROLE_ORDER, "generalist") if role in roles]
    rows: list[str] = []
    costs: dict[str, Decimal] = {}
    for role in ordered_roles:
        telemetry = roles[role]
        cost = _role_cost(telemetry, pricing_snapshot_ids.get(role))
        if cost is not None:
            costs[role] = cost
        rows.append(
            f"<tr><th>{_h(ROLE_LABELS[role])}</th>"
            f"<td>{_h(telemetry.model)}</td>"
            f"<td>{_duration(telemetry.duration_ms)}</td>"
            f"<td>{telemetry.usage.input_tokens:,}</td>"
            f"<td>{telemetry.usage.output_tokens:,}</td>"
            f"<td>{telemetry.usage.total_tokens:,}</td>"
            f"<td>{_h(_persisted_cost(cost, (() if cost is not None else (telemetry.model,))))}</td></tr>"
        )
    token_chart = _token_chart({role: roles[role] for role in ordered_roles})
    duration_chart = _duration_chart({role: roles[role] for role in ordered_roles})
    cost_chart = _cost_chart(costs) if costs else ""
    total_input = sum(roles[role].usage.input_tokens for role in ordered_roles)
    total_output = sum(roles[role].usage.output_tokens for role in ordered_roles)
    total_tokens = sum(roles[role].usage.total_tokens for role in ordered_roles)
    total_cost = sum(costs.values(), start=Decimal("0")) if len(costs) == len(ordered_roles) else None
    total_cost_text = _persisted_cost(
        total_cost,
        (() if total_cost is not None else tuple(roles[role].model for role in ordered_roles)),
    )
    return f"""
<section id="metrics">
  <h2>Metrics &amp; Charts</h2>
  <p class="section-note">Charts use persisted completed-call telemetry. Durations are shown by role; no relative execution start positions are inferred.</p>
  <div class="chart-grid">{token_chart}{duration_chart}{cost_chart}</div>
  <div class="table-wrap"><table>
    <thead><tr><th>Role</th><th>Model</th><th>Duration</th><th>Input Tokens</th><th>Output Tokens</th><th>Total Tokens</th><th>Estimated Cost</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
    <tfoot><tr><th>Role totals</th><td>&mdash;</td><td>Not summed</td><td>{total_input:,}</td><td>{total_output:,}</td><td>{total_tokens:,}</td><td>{_h(total_cost_text)}</td></tr></tfoot>
  </table></div>
</section>
"""


def _token_chart(roles: Mapping[str, RoleTelemetry]) -> str:
    maximum = max(
        (role.usage.input_tokens + role.usage.output_tokens for role in roles.values()),
        default=0,
    )
    bars = "".join(
        _stacked_bar(
            ROLE_LABELS[role],
            telemetry.usage.input_tokens,
            telemetry.usage.output_tokens,
            maximum,
        )
        for role, telemetry in roles.items()
    )
    return f'<article class="chart-card"><h3>Tokens by Role</h3><div class="legend"><span class="input-key">Input</span><span class="output-key">Output</span></div>{bars or _not_available()}</article>'


def _stacked_bar(
    label: str,
    input_tokens: int,
    output_tokens: int,
    maximum: int,
) -> str:
    input_width = (input_tokens / maximum * 100) if maximum else 0
    output_width = (output_tokens / maximum * 100) if maximum else 0
    return f"""
<div class="bar-row">
  <div class="bar-label"><span>{_h(label)}</span><b>{input_tokens + output_tokens:,}</b></div>
  <div class="bar-track" aria-label="{_h(label)}: {input_tokens:,} input tokens and {output_tokens:,} output tokens">
    <span class="bar input" style="width:{input_width:.3f}%"></span><span class="bar output" style="width:{output_width:.3f}%"></span>
  </div>
</div>"""


def _duration_chart(roles: Mapping[str, RoleTelemetry]) -> str:
    timed_roles = {
        role: telemetry
        for role, telemetry in roles.items()
        if telemetry.duration_ms is not None
    }
    maximum = max(
        (telemetry.duration_ms for telemetry in timed_roles.values()),
        default=0,
    )
    bars = "".join(
        _single_bar(
            ROLE_LABELS[role],
            telemetry.duration_ms,
            maximum,
            _duration(telemetry.duration_ms),
            "duration",
        )
        for role, telemetry in timed_roles.items()
    )
    return f'<article class="chart-card" id="duration-by-role"><h3>Execution Duration by Role</h3>{bars or _not_available()}</article>'


def _cost_chart(costs: Mapping[str, Decimal]) -> str:
    maximum = max(costs.values(), default=Decimal("0"))
    bars = "".join(
        _bar_with_width(
            ROLE_LABELS[role],
            _money(cost),
            "cost",
            _decimal_percentage(cost, maximum),
        )
        for role, cost in costs.items()
    )
    return f'<article class="chart-card"><h3>Estimated Cost by Role</h3>{bars}</article>'


def _single_bar(
    label: str,
    value: float,
    maximum: float,
    display: str,
    css_class: str,
) -> str:
    width = (value / maximum * 100) if maximum else 0
    return _bar_with_width(label, display, css_class, width)


def _bar_with_width(
    label: str,
    display: str,
    css_class: str,
    width: float | Decimal,
) -> str:
    return f"""
<div class="bar-row">
  <div class="bar-label"><span>{_h(label)}</span><b>{_h(display)}</b></div>
  <div class="bar-track" aria-label="{_h(label)}: {_h(display)}"><span class="bar {css_class}" style="width:{width:.3f}%"></span></div>
</div>"""


def _decimal_percentage(value: Decimal, maximum: Decimal) -> Decimal:
    if maximum <= 0:
        return Decimal("0")
    percentage = value / maximum * Decimal("100")
    return min(Decimal("100"), max(Decimal("0"), percentage))


def _risks_section(
    risks_and_unknowns: Iterable[str],
    *,
    title: str = "Risks & Unknowns",
) -> str:
    values = list(risks_and_unknowns)
    cards = "".join(
        f'<li><span class="risk-index">{index}</span><span>{_h(value)}</span></li>'
        for index, value in enumerate(values, start=1)
    )
    if not cards:
        cards = '<li class="not-available">Not available</li>'
    return f'<section id="risks"><h2>{_h(title)}</h2><ol class="risk-list">{cards}</ol><p class="section-note">No probability or impact score is inferred.</p></section>'


def _evidence_section(
    context: ContextBundle,
    specialists: Mapping[str, SpecialistCommon],
) -> str:
    context_cards = "".join(
        f"""
<article class="evidence-card">
  <div><code>{_h(item.id)}</code><strong>{_h(item.file_path)}</strong></div>
  <p><b>Selection:</b> {_h(', '.join(item.selection_reasons) or 'Not specified')}</p>
  <p><b>Matched terms:</b> {_h(', '.join(item.matched_terms) or 'None')}</p>
  <p><b>Excerpt truncated:</b> {_h(str(item.truncated))}</p>
</article>"""
        for item in context.evidence
    ) or _not_available()
    groups = "".join(
        _specialist_evidence_group(role, specialist)
        for role, specialist in specialists.items()
    )
    return f"""
<section id="evidence">
  <h2>Repository Evidence</h2>
  <p class="section-note">Bounded evidence metadata is shown without reproducing full repository source excerpts.</p>
  <div class="evidence-grid">{context_cards}</div>
  {groups}
</section>
"""


def _specialist_evidence_group(
    role: str,
    specialist: SpecialistCommon,
) -> str:
    items = "".join(
        f"""
<article class="evidence-item">
  <div>{_evidence_chips([item.id])}<span>{_h(item.source_type.value)}</span></div>
  <p>{_h(item.claim)}</p>
  <small>{_h(item.file_path or 'No repository path')} {_h(item.symbol or '')}</small>
  <p><b>Why it matters:</b> {_h(item.reason_it_matters)}</p>
</article>"""
        for item in specialist.evidence
    ) or _not_available()
    return f'<details><summary>{_h(ROLE_LABELS[role])} Evidence</summary><div class="evidence-grid">{items}</div></details>'


def _human_review_section(
    record: RunRecord,
    comparison: ComparisonRecord | None,
) -> str:
    decision = record.human_decision
    if decision is None:
        product = '<div class="pending-card"><strong>Product / Director Review</strong><p>Status: Pending</p></div>'
    else:
        product = f"""
<div class="card">
  <h3>Product / Director Review</h3>
  <dl class="facts">
    <dt>Action</dt><dd>{_h(decision.action.value)}</dd>
    <dt>Final decision</dt><dd>{_h(decision.final_decision.value if decision.final_decision else 'None')}</dd>
    <dt>Note</dt><dd>{_h(decision.note or 'None')}</dd>
    <dt>Timestamp</dt><dd>{_h(decision.timestamp.isoformat())}</dd>
  </dl>
</div>"""
    if comparison is None:
        comparison_review = '<div class="pending-card neutral"><strong>Council vs Generalist Review</strong><p>Not applicable for a Council-only run.</p></div>'
    elif comparison.human_review is None:
        comparison_review = '<div class="pending-card"><strong>Council vs Generalist Review</strong><p>Status: Pending</p></div>'
    else:
        review = comparison.human_review
        comparison_review = f"""
<div class="card">
  <h3>Council vs Generalist Review</h3>
  <p><b>Preference:</b> {_h(review.preference.value)}</p>
  <p><b>Reason:</b> {_h(review.reason)}</p>
  <p><b>Timestamp:</b> {_h(review.timestamp.isoformat())}</p>
</div>"""
    return f'<section id="human-review"><h2>Human Review</h2><div class="two-column">{product}{comparison_review}</div></section>'


def _generalist_human_review() -> str:
    return """
<section id="human-review">
  <h2>Human Review</h2>
  <div class="pending-card neutral"><strong>Product / Director Review</strong><p>Not applicable for a Generalist-only run.</p></div>
  <div class="pending-card neutral"><strong>Council vs Generalist Review</strong><p>Not applicable without both evaluation paths.</p></div>
</section>
"""


def _condition(label: str, values: Iterable[str]) -> str:
    return f'<article><h3>{_h(label)}</h3>{_text_list(values)}</article>'


def _flow_step(label: str, values: Iterable[str]) -> str:
    return f'<article><span>{_h(label)}</span>{_text_list(values)}</article>'


def _role_telemetry(telemetry: RoleTelemetry | None) -> str:
    if telemetry is None:
        return '<p class="not-available">Telemetry: Not available</p>'
    return f"""
<dl class="telemetry">
  <div><dt>Status</dt><dd>Complete</dd></div>
  <div><dt>Model</dt><dd>{_h(telemetry.model)}</dd></div>
  <div><dt>Duration</dt><dd>{_duration(telemetry.duration_ms)}</dd></div>
  <div><dt>Tokens</dt><dd>{telemetry.usage.total_tokens:,}</dd></div>
</dl>"""


def _role_cost(
    telemetry: RoleTelemetry,
    pricing_snapshot_id: str | None,
) -> Decimal | None:
    if pricing_snapshot_id != DEFAULT_PRICING_SNAPSHOT.identifier:
        return None
    return estimate_cost(
        {"role": telemetry},
        DEFAULT_PRICING_SNAPSHOT,
    ).estimated_cost_usd


def _persisted_cost(
    cost: Decimal | None,
    unpriced_models: Iterable[str],
) -> str:
    if cost is not None:
        return _money(cost)
    models = list(unpriced_models)
    return "Unpriced" if models else "Unavailable"


def _refinement_persisted_cost(
    refinement: FeatureRefinementRecord,
) -> str:
    if not refinement.usage_complete and not refinement.rounds:
        return "Unavailable"
    return _persisted_cost(
        refinement.estimated_cost_usd,
        refinement.unpriced_models,
    )


def _sum_complete_decimals(values: Iterable[Decimal | None]) -> Decimal | None:
    collected = list(values)
    if any(value is None for value in collected):
        return None
    return sum(
        (value for value in collected if value is not None),
        Decimal("0"),
    )


def _sum_known_decimals(values: Iterable[Decimal | None]) -> Decimal | None:
    known = [value for value in values if value is not None]
    return sum(known, Decimal("0")) if known else None


def _money(value: Decimal) -> str:
    return f"${value:.6f}"


def _duration(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.3f} ms"


def _effort(minimum: float | None, maximum: float | None) -> str:
    if minimum is None and maximum is None:
        return "Unavailable"
    low = str(minimum) if minimum is not None else "?"
    high = str(maximum) if maximum is not None else "?"
    return f"{low}-{high} developer days"


def _text_list(values: Iterable[str]) -> str:
    items = list(values)
    if not items:
        return '<p class="not-available">Not specified</p>'
    return "<ul>" + "".join(f"<li>{_h(item)}</li>" for item in items) + "</ul>"


def _numbered_list(values: Iterable[str]) -> str:
    items = list(values)
    if not items:
        return '<p class="not-available">Not specified</p>'
    return "<ol>" + "".join(f"<li>{_h(item)}</li>" for item in items) + "</ol>"


def _evidence_chips(evidence_ids: Iterable[str]) -> str:
    values = list(evidence_ids)
    if not values:
        return ""
    return '<span class="chips">' + "".join(
        f"<code>{_h(value)}</code>" for value in values
    ) + "</span>"


def _not_available() -> str:
    return '<p class="not-available">Not available</p>'


def _footer(pricing_snapshot_id: str) -> str:
    return f"""
<footer>
  <p>Deterministic presentation of persisted Game Feature Council artifacts.</p>
  <p>Pricing snapshot: <code>{_h(pricing_snapshot_id)}</code></p>
</footer>
"""


def _specialist_mapping(record: RunRecord) -> dict[str, SpecialistCommon]:
    result = record.council_result
    return {
        "game_design": result.game_design,
        "technical": result.technical,
        "analytics": result.analytics,
        "scope_risk": result.scope_risk,
    }


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _role_icon(role: str) -> str:
    paths = {
        "game_design": '<path d="M5 10h14l2 7-3 2-3-3H9l-3 3-3-2 2-7Zm4 2v4m-2-2h4m6-1h.01m2 2h.01"/>',
        "technical": '<path d="m9 7-5 5 5 5m6-10 5 5-5 5m-3-12-2 14"/>',
        "analytics": '<path d="M4 19V5m0 14h16M7 16l4-5 3 2 5-7"/>',
        "scope_risk": '<path d="M12 3 4 6v5c0 5 3 8 8 10 5-2 8-5 8-10V6l-8-3Zm0 5v5m0 3h.01"/>',
        "producer": '<path d="m12 3 9 5-9 5-9-5 9-5Zm-7 9 7 4 7-4m-14 4 7 4 7-4"/>',
        "director": '<circle cx="12" cy="12" r="9"/><path d="m15 9-2 4-4 2 2-4 4-2Z"/>',
    }
    path = paths.get(role, '<circle cx="12" cy="12" r="8"/>')
    return f'<svg class="role-icon" viewBox="0 0 24 24" aria-hidden="true">{path}</svg>'


_CSS = r"""
:root{color-scheme:dark;--bg:#08111f;--surface:#0f1b2d;--surface2:#14243a;--text:#edf5ff;--muted:#9fb0c7;--border:#29405d;--blue:#57a5ff;--cyan:#4dd7dd;--violet:#a78bfa;--amber:#f4bd62;--pink:#f28cc8;--green:#64d7a0;--slate:#aab8cb}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:radial-gradient(circle at 80% 0,#132846 0,transparent 38%),var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}.shell{display:grid;grid-template-columns:230px minmax(0,1fr);max-width:1600px;margin:auto}.sidebar{position:sticky;top:0;height:100vh;padding:28px 22px;border-right:1px solid var(--border);background:#091321e8}.brand-mark{display:grid;place-items:center;width:48px;height:48px;border:1px solid #4c6f98;border-radius:14px;background:#152942;color:#8fc4ff;font-weight:800;letter-spacing:.08em}.sidebar nav{display:flex;flex-direction:column;gap:4px;margin-top:28px}.sidebar a{padding:9px 10px;border-radius:8px;color:var(--muted);text-decoration:none}.sidebar a:hover,.sidebar a:focus{background:var(--surface2);color:var(--text)}main{min-width:0;padding:38px clamp(20px,4vw,64px) 70px}.hero{padding:20px 0 28px;border-bottom:1px solid var(--border)}.eyebrow,.decision-kicker{margin:0;color:#7db9ff;font-size:.78rem;font-weight:800;letter-spacing:.13em;text-transform:uppercase}h1{margin:.2rem 0;font-size:clamp(2.2rem,5vw,4.4rem);line-height:1.05}h2{margin:0 0 18px;font-size:1.6rem}h3{margin:.2rem 0 .7rem}h4{margin:1.1rem 0 .5rem}.run-meta{display:flex;flex-wrap:wrap;gap:12px 24px;margin-top:20px;color:var(--muted)}.run-meta b{margin-right:6px;color:var(--text)}section{margin:34px 0;padding:28px;border:1px solid var(--border);border-radius:18px;background:#0d192aeb;box-shadow:0 18px 50px #0003}.ai-disclaimer{margin:26px 0;padding:18px 20px;border:1px solid #8d672b;border-left:5px solid var(--amber);border-radius:12px;background:#2b2112;color:#f9e3b7}.ai-disclaimer strong{font-size:1.05rem}.ai-disclaimer p{margin:.35rem 0}.metric-grid,.role-grid,.chart-grid,.evidence-grid,.comparison-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}.metric{min-height:116px;padding:18px;border:1px solid var(--border);border-radius:14px;background:linear-gradient(145deg,#15263c,#101d30)}.metric span{display:block;color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.08em}.metric strong{display:block;margin-top:10px;overflow-wrap:anywhere;font-size:1.35rem}.context-strip,.two-column,.three-column{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.three-column{grid-template-columns:repeat(3,minmax(0,1fr))}.context-strip>div,.card,.pending-card,.chart-card,.evidence-card,.evidence-item{padding:20px;border:1px solid var(--border);border-radius:14px;background:var(--surface)}.prewrap{white-space:pre-wrap;overflow-wrap:anywhere}.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:7px 14px}.facts dt{color:var(--muted)}.facts dd{margin:0;overflow-wrap:anywhere}.decision-panel{background:linear-gradient(145deg,#123021,#0d1d25 68%)}.decision-panel.generalist{background:linear-gradient(145deg,#1a2637,#0d1d2c 68%)}.decision-value{margin:.3rem 0;font-size:clamp(2rem,5vw,4rem);font-weight:850;overflow-wrap:anywhere}.badge{display:inline-block;padding:5px 10px;border:1px solid #4b7d67;border-radius:999px;background:#153426}.condition-grid,.flow-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:22px}.condition-grid article,.flow-grid article{padding:15px;border:1px solid var(--border);border-radius:12px;background:#0a1626}.flow-grid article>span{display:block;color:#83bfff;font-size:.78rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.architecture{margin-top:22px;padding:20px;border:1px dashed #3e5877;border-radius:14px}.architecture-lane{display:flex;align-items:center;justify-content:center;gap:12px;flex-wrap:wrap}.architecture-lane+.architecture-lane{margin-top:15px}.arch-group,.arch-node{padding:12px 15px;border:1px solid var(--border);border-radius:10px;background:var(--surface2)}.arch-group{display:flex;flex-wrap:wrap;gap:8px}.arch-group b{width:100%}.arch-group span{padding:3px 7px;border-radius:6px;background:#0c192a;color:var(--muted)}.arrow{color:#75b7ff;font-size:1.5rem}.role-card{padding:20px;border:1px solid var(--border);border-top:3px solid var(--slate);border-radius:14px;background:var(--surface)}.role-card.violet{border-top-color:var(--violet)}.role-card.blue{border-top-color:var(--blue)}.role-card.cyan{border-top-color:var(--cyan)}.role-card.amber{border-top-color:var(--amber)}.role-card header,.section-heading{display:flex;align-items:center;gap:12px}.role-card header span,.section-heading p,.section-note{color:var(--muted)}.role-icon{width:34px;height:34px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}.telemetry{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.telemetry div{padding:8px;border-radius:8px;background:#0a1524}.telemetry dt{color:var(--muted);font-size:.75rem}.telemetry dd{margin:2px 0 0;overflow-wrap:anywhere}.finding-list li{margin:.55rem 0}.chips{display:inline-flex;flex-wrap:wrap;gap:4px;margin-left:8px}.chips code,.evidence-card code{padding:2px 6px;border:1px solid #365371;border-radius:999px;background:#15263a;color:#9ed0ff}.table-wrap{overflow-x:auto;margin-top:18px}table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid var(--border);text-align:left}thead th{color:#b8c9dd;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}.bar-row{margin:12px 0}.bar-label{display:flex;justify-content:space-between;gap:10px;margin-bottom:5px}.bar-label span{overflow-wrap:anywhere}.bar-track{display:flex;height:10px;overflow:hidden;border-radius:999px;background:#07101c}.bar{display:block;height:100%}.bar.input{background:var(--blue)}.bar.output{background:var(--violet)}.bar.duration{background:var(--cyan)}.bar.cost{background:var(--green)}.legend{display:flex;gap:16px;color:var(--muted);font-size:.82rem}.legend span:before{content:"";display:inline-block;width:9px;height:9px;margin-right:6px;border-radius:2px}.input-key:before{background:var(--blue)}.output-key:before{background:var(--violet)}.generalist-key:before{background:var(--cyan)}.council-key:before{background:var(--violet)}.risk-list{list-style:none;padding:0}.risk-list li{display:flex;gap:12px;margin:10px 0;padding:14px;border:1px solid var(--border);border-radius:10px;background:var(--surface)}.risk-index{display:grid;place-items:center;flex:0 0 28px;height:28px;border-radius:50%;background:#3a2b15;color:#ffd78c;font-weight:800}.evidence-card strong{display:block;margin-top:9px;overflow-wrap:anywhere}.evidence-item small{color:var(--muted)}details{margin-top:12px;border:1px solid var(--border);border-radius:10px;background:#0a1626}summary{cursor:pointer;padding:14px;font-weight:700}details>.evidence-grid{padding:0 14px 14px}.pending-card{border-style:dashed;border-color:#7d653d;background:#211c13}.pending-card.neutral{border-color:var(--border);background:var(--surface)}.radar-layout{display:grid;grid-template-columns:minmax(280px,400px) minmax(0,1fr);gap:24px;align-items:center;margin-top:20px}.radar{width:100%;max-width:380px}.radar-grid,.radar-axis{fill:none;stroke:#35506f;stroke-width:1}.radar-series{stroke-width:2}.generalist-series{fill:#4dd7dd44;stroke:var(--cyan)}.council-series{fill:#a78bfa55;stroke:var(--violet)}code{overflow-wrap:anywhere;color:#b9dcff}footer{margin-top:40px;padding:20px 0;border-top:1px solid var(--border);color:var(--muted)}.not-available{color:var(--muted);font-style:italic}
@media(max-width:900px){.shell{display:block}.sidebar{position:static;width:auto;height:auto;border-right:0;border-bottom:1px solid var(--border)}.brand-mark{display:none}.sidebar nav{flex-direction:row;overflow-x:auto;margin:0}.sidebar a{white-space:nowrap}main{padding:24px 16px}.condition-grid,.flow-grid,.three-column{grid-template-columns:repeat(2,minmax(0,1fr))}.radar-layout{grid-template-columns:1fr}}
@media(max-width:600px){.context-strip,.two-column,.three-column,.condition-grid,.flow-grid{grid-template-columns:1fr}section{padding:20px}.facts{grid-template-columns:1fr}.facts dd{margin-bottom:7px}}
@media print{:root{color-scheme:light}body{background:#fff;color:#111;font-size:11pt}.shell{display:block;max-width:none}.sidebar{display:none}main{padding:0}.hero,section,.ai-disclaimer,.card,.role-card,.chart-card,.evidence-card,.evidence-item,.pending-card{break-inside:avoid;background:#fff;color:#111;box-shadow:none;border-color:#888}.section-note,.run-meta,.role-card header span,footer{color:#444}.bar-track{border:1px solid #777;background:#eee}.chips code,.evidence-card code{color:#111;background:#eee}.ai-disclaimer{border-color:#9a6817}.decision-panel{background:#fff}.architecture,.architecture *{border-color:#888}a{color:#111}}
"""
