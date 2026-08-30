# Game Feature Council — V1 Architecture and Operating Contract

This document is the authoritative description of the V1 system. It describes
the implemented runtime contract, not a roadmap.

## Purpose

Game Feature Council helps a developer answer one question:

> What is the smallest credible experiment that tests the riskiest assumption
> behind this feature, given the repository that exists today?

The system produces a grounded recommendation and an experiment definition. It
does not make a production-release decision. Human Product/Director review
remains mandatory for Council recommendations.

## Architecture

One canonical feature brief and one `ContextBundle` are shared across the
selected evaluation paths. Interactive users may opt into a paid interpretation
step before any repository content is read or transmitted:

```text
original user request
        |
optional Feature Refiner (feature text only)
        |
user approval
        |
approved canonical feature + tracked Git working tree
                 |
           Context Builder (once)
                 |
        canonical ContextBundle
          /                 \
 four specialists       Generalist
    concurrently         (baseline)
          |
 validate specialist evidence
          |
       Producer
          |
 validate Producer evidence
          |
       Director
          |
     Council result
          \                 /
        optional human comparison
```

The four specialists run concurrently. Producer and Director are separate,
sequential synthesis stages. The Generalist is an independent baseline: it
receives the same feature text and the same context, but cannot see Council
outputs. The `both` CLI mode builds the context exactly once and supplies that
same bundle to both paths.

The fixed pipeline is intentionally implemented without agent handoffs,
free-form debate, or a separate orchestration framework.

The Refiner clarifies wording, preserves explicit constraints, and exposes
ambiguity; it does not inspect repository context, design implementation,
resolve evidence, or replace any specialist. Consent for this call defaults to
No and is separate from the later repository-context consent. Rejected output
may be corrected with another explicitly approved call, up to three total
Refiner calls. The original request and deterministic round history are stored
in `feature_refinement.json` only when the Refiner was used. `input.json`
records the canonical feature actually evaluated.

## Context Builder

The Context Builder creates deterministic, bounded repository evidence:

- The candidate set comes from `git ls-files`; arbitrary untracked files are
  excluded.
- Selected files are read normally from the current working tree, so tracked
  uncommitted changes are included.
- Git-native search and deterministic relevance ranking select the evidence.
- Reads are bounded per file and across the complete bundle.
- Each selected excerpt receives a stable manifest identifier such as
  `repo-001`.
- Specialist and Producer repository evidence IDs are validated against the
  exact bundle before the next synthesis stage runs.
- The bundle records the absolute repository path, `HEAD` commit SHA, current
  branch, and whether tracked files are dirty.

The practical source identity is repository path, `HEAD` SHA, tracked-working-
tree dirty state, feature input, and Context Builder configuration. A clean
checkout can reproduce source contents from its SHA. A dirty checkout cannot
be reproduced from the SHA alone.

Current V1 defaults are:

| Limit | Default |
| --- | ---: |
| Search terms | 12 |
| Selected files | 10 |
| Characters per file | 8,000 |
| Total context characters | 50,000 |

These are source limits, not model token guarantees. The builder does not use
embeddings, a vector database, an LLM selector, or retrieval-augmented
generation infrastructure.

Git's dubious-ownership checks remain in force. The system does not add a
`safe.directory` entry, inject a trust override, or modify Git configuration.
The caller must resolve repository ownership or trust configuration.

## Agent responsibilities

| Role | Responsibility |
| --- | --- |
| Game Design | Clarify the player problem, behavior hypothesis, mechanic, and smallest meaningful player-facing test. |
| Technical | Identify grounded implementation reuse, missing evidence, integration risks, and an evidence-based effort range. |
| Analytics | Define exposure, behavior, outcome, guardrail, and validity signals needed to interpret the experiment. |
| Scope/Risk | Reduce scope, identify dependencies and failure modes, and protect reversibility and player trust. |
| Producer | Reconcile the specialist outputs into a consolidated, evidence-grounded experiment proposal while preserving disagreements. |
| Director | Make the final AI recommendation, state confidence and effort, define decision conditions, and identify questions requiring human resolution. |
| Generalist | Produce an independent end-to-end baseline from the same feature and context for human comparison. |

The Director decision vocabulary is `GO`, `GO_WITH_REDUCED_SCOPE`,
`PROTOTYPE_FIRST`, `NEEDS_MORE_INFORMATION`, and `DO_NOT_BUILD_YET`. Every
Director result requires human approval; none means release to production.

## Lean experiment contract

A useful recommendation connects the learning loop:

1. **Build:** implement only the smallest experiment needed to test the
   riskiest assumption, plus the minimum state and measurement needed to
   interpret it.
2. **Measure:** observe opportunity, participation, behavior, outcomes,
   guardrails, and experiment validity.
3. **Learn:** distinguish evidence about the product hypothesis from failures
   of assignment, eligibility, persistence, implementation, or measurement.
4. **Decide:** scale, iterate, kill, or collect more data using explicit
   conditions.

Validity has precedence over product interpretation:

| Evidence state | Decision condition |
| --- | --- |
| Trustworthy positive evidence | Scale |
| Trustworthy, useful evidence that calls for a change | Iterate |
| Trustworthy negative evidence from a valid experiment | Kill |
| Broken or uninterpretable implementation, assignment, persistence, eligibility, or instrumentation | Need more data |

Agents must not invent numeric success thresholds that are absent from the
feature brief and repository evidence. A missing Product threshold remains an
explicit human decision.

## Effort semantics

The existing `EffortRange` JSON shape is used consistently:

- `developer_days_min: null` and `developer_days_max: null` means the numeric
  estimate is unavailable. Its confidence is always normalized to `low`.
- `developer_days_min: 0` and `developer_days_max: 0` means a known conclusion
  that no developer implementation work is required. Its supplied confidence
  is preserved.
- A non-zero numeric range preserves its supplied confidence.

The rule is independent of free-text wording in `basis`; runtime code does not
classify English phrases to infer effort meaning.

## Controlled execution, telemetry, and tracing

All model calls pass through one controlled execution boundary. That boundary
supplies the Agents SDK run configuration and returns SDK token usage. The
orchestrator records local call duration and the reporting layer calculates
cost using the project's dated pricing snapshot. Local duration, token, and
cost telemetry does not depend on SDK tracing.

Model selection is configuration-controlled:

- `COUNCIL_SPECIALIST_MODEL` selects the four specialist models.
- `COUNCIL_SYNTHESIS_MODEL` selects Producer, Director, and Generalist.
- `COUNCIL_REFINER_MODEL` selects the optional interactive Feature Refiner; if
  unset, it falls back to the exact synthesis model configuration.

The model names in README setup examples are examples, not hard-coded runtime
defaults. A cost cap fails safely before execution when a selected model is not
priced in the local snapshot. For interactive refined runs, the cap covers the
cumulative actual estimated cost of completed Refiner calls plus the
conservative downstream estimate. Unknown or unpriced completed usage fails
closed when a cap is configured.

Agents SDK tracing is explicitly disabled for every model call by default.
`COUNCIL_ENABLE_TRACING=1` opts into tracing while sensitive generation and
tool data remains excluded. Sensitive tracing is enabled only when both that
variable and `COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA=1` are set. Human review is
offline and tracing-independent, and trace IDs are not persisted in artifacts.

## Safety boundaries

- Repository access is read-only: no source writes, Git writes, Git
  configuration changes, Unity invocation, or service deployment.
- The output root must resolve outside the analyzed repository, including
  traversal and symlink-equivalent paths.
- Preflight and dry-run make no model calls.
- Real execution requires an API key, cost-cap checks, and explicit consent;
  `--yes` skips only the interactive prompt.
- User-facing CLI errors redact every exact occurrence of the configured API
  key, including embedded and repeated occurrences.
- Repository-derived context is sent to the configured API provider only after
  consent. The API key is neither printed nor persisted.
- Partial failures return non-zero and never print a successful completion
  summary.

## CLI behavior

The two top-level commands are:

```text
python -m council run --repo PATH --feature-file FILE [options]
python -m council review --run RUN_ID [--output-dir PATH]
```

`run` supports `--mode council`, `--mode generalist`, and `--mode both`; `both`
is the default. Nominal model-call counts are six, one, and seven respectively.
Retries are not included in those nominal counts. `--dry-run` validates the
repository and feature, builds the context, and prints the complete approximate
preflight without model calls or artifacts. `--max-cost-usd` is enforced before
provider execution, and `--yes` cannot bypass it.

`review` reads existing artifacts only. It imports no agent execution path,
requires no API key or model configuration, does not inspect the target
repository, and makes no model or Context Builder call. It records pending
Product/Director accept, reject, or modify decisions and, when a comparison
exists, the pending Council-vs-Generalist human review. Completed reviews
cannot be silently replaced.

## Run artifacts

Each run uses a dedicated directory beneath the selected output root.

| Mode | Files |
| --- | --- |
| Council | `input.json`, `context.json`, `game_design.json`, `technical.json`, `analytics.json`, `scope_risk.json`, `producer.json`, `director.json`, `run.json`, `report.md`, `report.html` |
| Both | All Council files plus `generalist.json` and `comparison.json` |
| Generalist | `input.json`, `context.json`, `generalist.json`, `report.md`, `report.html` |

Structured JSON preserves the complete agent outputs. `report.md` is the
human-facing projection; it deterministically removes exact and safely
normalized duplicate risks while keeping materially distinct items. This uses
no model call and does not alter raw JSON. `report.html` is a deterministic,
self-contained visual projection of the same persisted data. It loads no
external resources and does not infer missing scores, metrics, or conclusions.

A Product/Director review changes `run.json`, `report.md`, and an existing
`report.html`. A comparison review changes `comparison.json`, `report.md`, and
an existing `report.html`. Review does not add HTML to historical runs that do
not already have it. If a Product decision is validly persisted before a later
comparison update fails, it remains valid; the CLI returns non-zero and does
not claim the whole review completed.

## V1 non-goals

V1 does not:

- autonomously approve or release a feature;
- implement the proposed game feature;
- invoke the game engine, deploy backend services, or change a repository;
- crawl untracked files or bypass Git ownership safety;
- perform autonomous agent handoffs, debate, or framework-managed planning;
- use embeddings, vector search, or an LLM to select repository context;
- invent missing business thresholds, analytics capability, or technical reuse;
- provide retry orchestration or promise that nominal call counts include
  provider retries;
- replace human Product/Director judgment or human Council-vs-Generalist
  evaluation.
