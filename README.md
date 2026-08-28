# Game Feature Council

Game Feature Council evaluates a proposed game feature against a bounded,
read-only snapshot of a Git working tree. It can run the multi-agent Council,
the single-agent Generalist baseline, or both against the exact same
`ContextBundle`.

## Setup

The supported runtime is Python 3.11 or 3.12; the audited Windows environment
uses Python 3.12. Create and activate a PowerShell virtual environment, then
install the project in editable mode:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

The dependency declaration pins the direct runtime dependencies to the audited
prototype versions. Exact pins avoid an unreviewed Agents SDK or validation
behavior change; update them deliberately with the test suite.

Activation is optional. The direct-interpreter form avoids accidentally using
system Python, which may not have the Agents SDK installed:

```powershell
.\.venv\Scripts\python.exe -m council --help
```

If `python -m council` reports `No module named 'agents'`, use the direct
interpreter above or activate `.venv` in the current PowerShell session.

Configure real runs with process-scoped PowerShell environment variables:

```powershell
$env:OPENAI_API_KEY = "..."
$env:COUNCIL_SPECIALIST_MODEL = "gpt-5.4-nano"
$env:COUNCIL_SYNTHESIS_MODEL = "gpt-5.4-nano"
```

Do not put the API key in committed files. These assignments last only for the
current process and its children. `review` needs none of these variables. A
dry run needs the model configuration used by its selected mode, but no API
key. A real run also requires `OPENAI_API_KEY`; Council and `both` modes require
both model variables, while Generalist mode requires only the synthesis model.
The API key is never written to run artifacts or printed by the CLI.

## Feature input

The committed project-agnostic example is `examples/feature.txt`. It includes a
player/product problem, feature idea, goal, constraints, open questions, and
explicitly missing decision thresholds.

The file must exist and contain non-whitespace text. Its content is passed
unchanged to every selected evaluation path.

## Dry run

```powershell
.\.venv\Scripts\python.exe -m council run `
  --repo "C:\path\to\game-repository" `
  --feature-file ".\examples\feature.txt" `
  --mode both `
  --dry-run
```

A dry run validates the inputs, inspects the Git working tree, builds one
bounded `ContextBundle`, and prints the model, call-count, token, and cost
preflight. It makes no model calls and creates no run artifacts.

## Real runs

Council only:

```powershell
.\.venv\Scripts\python.exe -m council run --repo "C:\path\to\game-repository" --feature-file ".\examples\feature.txt" --mode council
```

Generalist only:

```powershell
.\.venv\Scripts\python.exe -m council run --repo "C:\path\to\game-repository" --feature-file ".\examples\feature.txt" --mode generalist
```

Council and Generalist, using the same canonical context:

```powershell
.\.venv\Scripts\python.exe -m council run --repo "C:\path\to\game-repository" --feature-file ".\examples\feature.txt" --mode both
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

Complete pending human review from the CLI with:

```powershell
.\.venv\Scripts\python.exe -m council review --run <run-id>
```

Use `--output-dir` when the run was written beneath a non-default artifact
root. Review is artifact-only: it requires no API key or model configuration
and makes no model/API calls. The human controls the Product/Director A/R/M
decision and manually enters all Council-vs-Generalist rubric scores,
preference, and reason. Existing completed reviews are reported but are not
silently replaced.

## Privacy

Real runs send the selected repository-derived excerpts in the bounded
`ContextBundle` to the configured API provider. Use `--dry-run` to inspect the
selected context metadata, model configuration, and estimated cost before
granting consent. Untracked files are not selected by the Context Builder.

Agents SDK tracing is a separate export path and is explicitly disabled for
every CLI model call by default. The project passes a per-run SDK configuration,
so ambient SDK tracing defaults cannot silently enable it. Local RunRecord
token, cost, and latency telemetry remains enabled and does not depend on SDK
tracing.

To opt into SDK tracing without repository/model inputs and outputs:

```powershell
$env:COUNCIL_ENABLE_TRACING = "1"
```

This may export workflow/span metadata and operational details to the tracing
backend. Sensitive generation and tool data remains excluded. To deliberately
include that data as well, set both variables:

```powershell
$env:COUNCIL_ENABLE_TRACING = "1"
$env:COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA = "1"
```

That second opt-in may export the feature brief, selected repository-derived
context, model inputs, and model outputs. Remove the variables to restore the
safe default:

```powershell
Remove-Item Env:COUNCIL_ENABLE_TRACING -ErrorAction SilentlyContinue
Remove-Item Env:COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA -ErrorAction SilentlyContinue
```

Human `review` remains offline and tracing-independent. Trace IDs are not
persisted in run artifacts. See the
[Agents SDK tracing documentation](https://openai.github.io/openai-agents-python/tracing/)
for the underlying SDK behavior.
