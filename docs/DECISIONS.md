# V1 Decision Log

This compact log records the architecture and operating decisions implemented
in V1. Later changes should add a new entry or explicitly supersede an existing
one rather than silently rewriting the contract.

## D001 — Use deterministic bounded repository context

**Decision:** Build a `ContextBundle` with Git-native discovery and search,
deterministic ranking, stable evidence IDs, and bounded normal file reads.

**Reason:** Recommendations need inspectable repository grounding without an
embedding service, vector database, or model-selected retrieval layer.

**Consequence:** Selection is reproducible for the same practical source
identity and configuration, but it is deliberately narrower than semantic RAG.

## D002 — Read the current tracked working tree

**Decision:** Use `git ls-files` as the candidate set, read selected files from
the current filesystem, exclude untracked files, and record `HEAD`, branch, and
tracked dirty state.

**Reason:** The developer needs advice about checked-out code, including tracked
uncommitted edits, without ingesting arbitrary local files.

**Consequence:** A dirty bundle is not reproducible from the commit SHA alone.

## D003 — Preserve Git ownership safety

**Decision:** Do not configure `safe.directory` or inject a Git trust override.

**Reason:** Repository ownership is a caller-controlled security decision.

**Consequence:** Dubious ownership produces a clear failure that the caller
must resolve outside the tool.

## D004 — Validate repository evidence between stages

**Decision:** Validate all specialist evidence IDs and then Producer evidence
IDs against the exact `ContextBundle` manifest before downstream synthesis.

**Reason:** A later stage must not turn an invented repository reference into
an apparently grounded conclusion.

**Consequence:** Invalid specialist evidence blocks Producer; invalid Producer
evidence blocks Director.

## D005 — Use four fixed specialists concurrently

**Decision:** Run Game Design, Technical, Analytics, and Scope/Risk concurrently.

**Reason:** These perspectives cover the major product-learning and delivery
risks while keeping the V1 topology predictable.

**Consequence:** Specialist count and responsibilities are fixed, and Council
has four nominal specialist calls.

## D006 — Keep Producer and Director separate

**Decision:** Producer consolidates evidence and disagreements; Director makes
the AI recommendation, effort statement, experiment conditions, and human
questions in a subsequent call.

**Reason:** Separating synthesis from judgment makes the decision trail easier
to inspect and test.

**Consequence:** Council uses six nominal calls in total rather than collapsing
synthesis into one response.

## D007 — Keep a single-agent Generalist comparator

**Decision:** Run an independent Generalist against the exact same canonical
feature and context when comparison is requested.

**Reason:** V1 must test whether specialist structure adds human decision value
relative to a faster, cheaper baseline.

**Consequence:** Generalist uses one nominal call; `both` uses seven. The
Generalist cannot inspect Council outputs.

## D008 — Avoid framework handoffs and autonomous debate

**Decision:** Implement a fixed Python orchestration sequence without agent
handoffs, debate loops, or an additional orchestration framework.

**Reason:** The required workflow is small, deterministic, and easier to audit
directly.

**Consequence:** V1 does not dynamically create roles or change topology during
a run.

## D009 — Bound V1 to recommendation and experiment design

**Decision:** Generate grounded analysis, a smallest experiment, and decision
conditions; do not implement or release game features.

**Reason:** The goal is better pre-implementation learning, not autonomous
software delivery.

**Consequence:** No Unity invocation, service deployment, Git write, or target
repository mutation belongs in the runtime.

## D010 — Reserve final authority for humans

**Decision:** Every Council recommendation requires a Product/Director human
accept, reject, or modify action. No Director enum represents production
release approval.

**Reason:** Model output is advisory and may contain uncertainty or unsupported
assumptions.

**Consequence:** Reports distinguish pending questions from questions resolved
by the recorded human decision while preserving original Director JSON.

## D011 — Configure model roles through the environment

**Decision:** Use `COUNCIL_SPECIALIST_MODEL` for specialists and
`COUNCIL_SYNTHESIS_MODEL` for Producer, Director, and Generalist.

**Reason:** Model choice should be explicit and changeable without editing
prompts or orchestration code.

**Consequence:** README model names are configuration examples, not hard-coded
runtime defaults. Generalist-only mode does not require specialist config.

## D012 — Make cost safety pre-execution and fail closed

**Decision:** Print deterministic approximate preflight estimates, enforce
`--max-cost-usd` before provider calls, and reject capped runs using an unpriced
model.

**Reason:** Consent should be informed and an unknown price must not be treated
as zero.

**Consequence:** `--yes` bypasses only interactive consent, not the cost cap;
nominal call counts do not promise or budget provider retries.

## D013 — Tune Context Builder limits from repository validation

**Decision:** Keep current defaults at 12 search terms, 10 selected files,
8,000 characters per file, and 50,000 total characters.

**Reason:** Initial source limits were reduced and relevance behavior tuned
after real-repository validation showed that broader input added noise without
improving the essential evidence set.

**Consequence:** Defaults favor focused evidence; callers can still supply an
explicit `ContextBuilderConfig` for a different bounded workload.

## D014 — Give null effort bounds one deterministic meaning

**Decision:** `null/null` means an unavailable numeric estimate and always has
low confidence. Known zero implementation effort is `0/0`. Numeric ranges keep
their supplied confidence.

**Reason:** Runtime meaning must not depend on classifying arbitrary English in
the effort basis.

**Consequence:** The public JSON shape is unchanged, but the old ambiguity of
null as either unknown or zero is removed.

## D015 — Give experiment validity precedence over outcome

**Decision:** Broken assignment, implementation, eligibility, persistence, or
instrumentation maps to `need_more_data`; `kill` requires trustworthy negative
evidence from a valid experiment.

**Reason:** An uninterpretable experiment cannot establish that the product
hypothesis failed.

**Consequence:** Positive evidence can support scale, useful evidence can
support iteration, and credible valid negative evidence can support kill.

## D016 — Preserve raw outputs and deduplicate only the report

**Decision:** Keep all specialist risks, unknowns, disagreements, and evidence
in structured JSON. Deterministically deduplicate equivalent human-facing risk
lines in `report.md` without a model call.

**Reason:** Human reports need less repetition without losing the auditable
source outputs.

**Consequence:** Unicode-aware NFKC, case folding, punctuation/spacing
normalization, and conservative equivalence determine duplicates; materially
distinct entries remain and Producer wording wins only when equivalent.

## D017 — Make human review artifact-only and append-only in meaning

**Decision:** `council review` reads existing run artifacts, records pending
Product and comparison reviews, and refuses to silently replace completed
reviews.

**Reason:** Human review must be possible offline without paying for or changing
the AI run.

**Consequence:** No agent, Context Builder, target-repository inspection, model
configuration, or API key is required. A valid Product decision is not rolled
back if a later comparison update fails.

## D018 — Centralize controlled execution and default tracing off

**Decision:** Route every model call through one execution boundary and pass a
per-run SDK configuration with tracing disabled unless explicitly enabled.
Sensitive trace data requires a second explicit opt-in.

**Reason:** Ambient SDK defaults must not silently export repository-derived
inputs or model outputs.

**Consequence:** `COUNCIL_ENABLE_TRACING` opts into operational tracing;
`COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA` is effective only with tracing enabled.
Local token, latency, and cost telemetry remains independent.

## D019 — Pin the reproducible runtime surface

**Decision:** Support Python 3.11 and 3.12 and exactly pin the direct Agents SDK
and validation-library dependencies used by the prototype.

**Reason:** Unreviewed SDK or validation changes could alter execution,
structured outputs, telemetry, or privacy behavior.

**Consequence:** Dependency upgrades are deliberate changes that require the
full test suite and contract review.

## D020 — Use per-role run artifacts

**Decision:** Persist input and context separately, each specialist separately,
Producer and Director separately, plus `run.json` and `report.md`; comparison
artifacts are added only when applicable.

**Reason:** Per-role files are easier to validate, review, and preserve than an
earlier aggregate-output proposal.

**Consequence:** Council, combined, and Generalist-only runs have distinct,
explicit artifact contracts and never fabricate inapplicable Council fields.

## D021 — Resolve repository-answerable concerns before human review

**Decision:** Add one Evidence Resolver stage after specialist validation and
before Producer. It consolidates deterministic specialist concern provenance,
uses supplied evidence first, and may make one bounded fixed-string search of
tracked working-tree files before a second finalization pass.

**Consequence:** Council preflight reserves one to two Resolver calls and
persists `evidence_resolver.json`; Generalist still receives only the original
feature and ContextBundle. Human questions are restricted to genuine Product
decisions or repository-help limitations.

## D021 — Consolidate V1 documentation

**Decision:** Keep README as a short getting-started guide and consolidate the
authoritative V1 architecture, evaluation protocol, and decisions into these
three documents.

**Reason:** The implementation evolved through several milestones; a small
canonical set is easier to verify than scattered historical notes.

**Consequence:** Future documentation changes should update the appropriate
canonical document and record material contract changes here.
