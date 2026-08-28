# Game Feature Council

Game Feature Council evaluates a proposed game feature against a bounded,
read-only snapshot of a Git working tree. It can run the multi-agent Council,
the single-agent Generalist baseline, or both against the exact same
`ContextBundle`.

## Setup

Use Python 3.11 or newer. Create a virtual environment and install the project
runtime dependencies, including `openai-agents` and Pydantic.

```text
python -m venv .venv
# Activate .venv for your shell, then:
python -m pip install openai-agents pydantic
```

Configure the models through environment variables:

```text
COUNCIL_SPECIALIST_MODEL=gpt-5.4-nano
COUNCIL_SYNTHESIS_MODEL=gpt-5.4-nano
OPENAI_API_KEY=<your API key>
```

`OPENAI_API_KEY` is required only for a real run. It is never written to run
artifacts or printed by the CLI.

## Feature input

Create a UTF-8 plain-text feature brief, for example `feature.txt`:

```text
Feature: Cooperative challenge prototype

Player problem:
Players lack a lightweight reason to coordinate during short sessions.

Hypothesis:
A visible shared objective will increase meaningful cooperative participation.

Constraints:
Use existing progression and reward capabilities where repository evidence
supports them. Prefer the smallest credible experiment.
```

The file must exist and contain non-whitespace text. Its content is passed
unchanged to every selected evaluation path.

## Dry run

```text
python -m council run --repo "path/to/repository" --feature-file "feature.txt" --mode both --dry-run
```

A dry run validates the inputs, inspects the Git working tree, builds one
bounded `ContextBundle`, and prints the model, call-count, token, and cost
preflight. It makes no model calls and creates no run artifacts.

## Real runs

Council only:

```text
python -m council run --repo "path/to/repository" --feature-file "feature.txt" --mode council
```

Generalist only:

```text
python -m council run --repo "path/to/repository" --feature-file "feature.txt" --mode generalist
```

Council and Generalist, using the same canonical context:

```text
python -m council run --repo "path/to/repository" --feature-file "feature.txt" --mode both
```

`both` is the default mode. Use `--output-dir` to select an artifact root;
otherwise runs are written beneath `runs/` relative to the current directory.
The output root must not be inside the analyzed repository.

## Cost and consent

Every invocation prints a deterministic preflight estimate before any API
call. The estimate is intentionally rough; persisted telemetry is authoritative.

Use a conservative cost ceiling when desired:

```text
python -m council run --repo "path/to/repository" --feature-file "feature.txt" --max-cost-usd 0.10
```

If the conservative maximum exceeds the ceiling, or the configured model is
not present in the pricing snapshot, execution stops before model calls.

Interactive real runs display the external-data notice and default to No:

```text
Repository-derived context will be sent to the OpenAI API and may incur paid usage.
Proceed? [Y/N]
```

Use `--yes` only when explicit non-interactive consent has already been given.

## Artifacts

Council runs retain the established artifacts: input, context, four specialist
results, Producer, Director, run metadata, and `report.md`. Combined runs also
include `generalist.json` and `comparison.json`, with human comparison pending.

Standalone Generalist runs contain:

- `input.json`
- `context.json`
- `generalist.json`
- `report.md`

The CLI never writes to the analyzed repository and does not invoke Unity,
deploy services, or change Git configuration.

## Human review

AI recommendations never constitute production approval. For Council and
combined runs, the existing library-level review workflow records
Product/Director accept, reject, or modify decisions through
`prompt_for_human_decision` and `update_run_with_human_decision`.

For combined runs, the existing comparison workflow records the eight rubric
scores, Generalist/Council/Tie preference, and required reason through
`prompt_for_comparison_review` and `update_comparison_with_human_review`.
Review updates persist into the existing JSON/report artifacts without
rerunning models.

## Privacy

Real runs send the selected repository-derived excerpts in the bounded
`ContextBundle` to the configured API provider. Use `--dry-run` to inspect the
selected context metadata, model configuration, and estimated cost before
granting consent. Untracked files are not selected by the Context Builder.
