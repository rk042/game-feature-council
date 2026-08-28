import ast
import os
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from council.cli import parse_args
from council.execution import execute_agent
from council.tracing import build_agents_run_config


ROOT = Path(__file__).resolve().parent
README_PATH = ROOT / "README.md"
FEATURE_EXAMPLE_PATH = ROOT / "examples" / "feature.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"
APPROVED_EXECUTION_PATH = Path("council/execution.py")
SMOKE_SCRIPT_PATHS = (
    Path("test_agent.py"),
    Path("test_technical_agent.py"),
    Path("test_analytics_agent.py"),
    Path("test_scope_risk_agent.py"),
    Path("test_producer_agent.py"),
    Path("test_director_agent.py"),
)
RUNNER_EXECUTION_METHODS = frozenset({"run", "run_sync", "run_streamed"})
EXCLUDED_SOURCE_DIRECTORIES = frozenset(
    {".git", ".local", ".venv", "__pycache__", "build", "dist", "runs"}
)


class ReproducibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_dependency_metadata_declares_audited_direct_runtime_inputs(
        self,
    ) -> None:
        with PYPROJECT_PATH.open("rb") as pyproject_file:
            pyproject = tomllib.load(pyproject_file)

        project = pyproject["project"]
        self.assertEqual(project["requires-python"], ">=3.11,<3.13")
        self.assertEqual(
            set(project["dependencies"]),
            {
                "openai-agents==0.22.0",
                "pydantic==2.13.4",
            },
        )
        self.assertFalse(
            any(
                dependency.startswith("openai==")
                for dependency in project["dependencies"]
            ),
            "The transitive openai package must not be declared as direct.",
        )

    def test_generic_feature_example_is_nonempty_and_project_agnostic(
        self,
    ) -> None:
        example = FEATURE_EXAMPLE_PATH.read_text(encoding="utf-8")
        normalized = example.casefold()

        self.assertTrue(example.strip())
        for section in (
            "player / product problem:",
            "feature idea:",
            "product goal:",
            "constraints:",
            "open questions:",
            "numeric thresholds:",
        ):
            self.assertIn(section, normalized)
        self.assertIn("no decision thresholds are supplied", normalized)
        for private_name in (
            "gemhunter",
            "gem hunter",
            "weekly reward pressure",
            "wave-master",
        ):
            self.assertNotIn(private_name, normalized)

    def test_readme_windows_commands_match_cli_contract(self) -> None:
        readme = README_PATH.read_text(encoding="utf-8")

        for command in (
            "py -3.12 -m venv .venv",
            r".\.venv\Scripts\Activate.ps1",
            "python -m pip install -e .",
            r".\.venv\Scripts\python.exe -m council --help",
            r'$env:OPENAI_API_KEY = "..."',
            r'$env:COUNCIL_SPECIALIST_MODEL = "gpt-5.4-nano"',
            r'$env:COUNCIL_SYNTHESIS_MODEL = "gpt-5.4-nano"',
            r'--feature-file ".\examples\feature.txt"',
            "--mode both",
            "--dry-run",
            "-m council review --run <run-id>",
            '$env:COUNCIL_ENABLE_TRACING = "1"',
            '$env:COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA = "1"',
            "feature brief, selected repository-derived",
        ):
            self.assertIn(command, readme)

        parsed = parse_args(
            [
                "run",
                "--repo",
                r"C:\path\to\game-repository",
                "--feature-file",
                r".\examples\feature.txt",
                "--mode",
                "both",
                "--dry-run",
            ]
        )
        self.assertEqual(parsed.command, "run")
        self.assertEqual(parsed.mode, "both")
        self.assertTrue(parsed.dry_run)

    async def test_default_runner_config_disables_tracing_explicitly(
        self,
    ) -> None:
        sdk_result = SimpleNamespace(
            final_output="synthetic output",
            context_wrapper=SimpleNamespace(
                usage=SimpleNamespace(
                    requests=1,
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                )
            ),
        )
        runner = AsyncMock(return_value=sdk_result)
        ambient_sdk_defaults = {
            "OPENAI_AGENTS_DISABLE_TRACING": "0",
            "OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA": "1",
        }

        with (
            patch.dict(os.environ, ambient_sdk_defaults, clear=True),
            patch("council.execution.Runner.run", runner),
        ):
            execution = await execute_agent(
                "synthetic agent",
                "repository-derived input",
            )

        run_config = runner.await_args.kwargs["run_config"]
        self.assertTrue(run_config.tracing_disabled)
        self.assertFalse(run_config.trace_include_sensitive_data)
        self.assertEqual(run_config.workflow_name, "Game Feature Council")
        self.assertEqual(execution.usage.total_tokens, 15)

    def test_tracing_and_sensitive_data_each_require_explicit_opt_in(
        self,
    ) -> None:
        tracing_only = build_agents_run_config(
            {"COUNCIL_ENABLE_TRACING": "1"}
        )
        tracing_with_data = build_agents_run_config(
            {
                "COUNCIL_ENABLE_TRACING": "true",
                "COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA": "yes",
            }
        )
        sensitive_without_tracing = build_agents_run_config(
            {"COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA": "1"}
        )

        self.assertFalse(tracing_only.tracing_disabled)
        self.assertFalse(tracing_only.trace_include_sensitive_data)
        self.assertFalse(tracing_with_data.tracing_disabled)
        self.assertTrue(tracing_with_data.trace_include_sensitive_data)
        self.assertTrue(sensitive_without_tracing.tracing_disabled)
        self.assertFalse(
            sensitive_without_tracing.trace_include_sensitive_data
        )

    def test_boolean_environment_values_are_deterministic_and_fail_safe(
        self,
    ) -> None:
        cases = (
            (None, False),
            ("0", False),
            ("1", True),
            ("false", False),
            ("true", True),
            ("  true  ", True),
            ("  ", False),
            ("unexpected", False),
        )

        for value, expected_enabled in cases:
            with self.subTest(value=value):
                environment = (
                    {}
                    if value is None
                    else {"COUNCIL_ENABLE_TRACING": value}
                )
                config = build_agents_run_config(environment)
                self.assertEqual(
                    not config.tracing_disabled,
                    expected_enabled,
                )
                self.assertFalse(config.trace_include_sensitive_data)

        unexpected_sensitive = build_agents_run_config(
            {
                "COUNCIL_ENABLE_TRACING": "1",
                "COUNCIL_TRACE_INCLUDE_SENSITIVE_DATA": "unexpected",
            }
        )
        self.assertFalse(unexpected_sensitive.trace_include_sensitive_data)

    def test_only_approved_module_calls_agents_sdk_runner(self) -> None:
        approved_calls: list[tuple[str, int, str]] = []
        violations: list[tuple[str, int, str]] = []

        for path in _project_python_sources():
            relative_path = path.relative_to(ROOT)
            for line_number, method_name in _direct_runner_calls(path):
                call = (relative_path.as_posix(), line_number, method_name)
                if relative_path == APPROVED_EXECUTION_PATH:
                    approved_calls.append(call)
                else:
                    violations.append(call)

        self.assertEqual(
            violations,
            [],
            "Direct Agents SDK Runner calls outside the approved execution "
            f"boundary: {violations}",
        )
        self.assertEqual(len(approved_calls), 1)
        self.assertEqual(approved_calls[0][0], APPROVED_EXECUTION_PATH.as_posix())
        self.assertEqual(approved_calls[0][2], "run")

    def test_all_executable_smoke_scripts_use_controlled_boundary(self) -> None:
        for relative_path in SMOKE_SCRIPT_PATHS:
            with self.subTest(script=relative_path.as_posix()):
                tree = ast.parse(
                    (ROOT / relative_path).read_text(encoding="utf-8-sig"),
                    filename=relative_path.as_posix(),
                )
                imported_names = {
                    alias.asname or alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.module == "council.execution"
                    for alias in node.names
                    if alias.name == "execute_agent"
                }
                calls = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in imported_names
                ]

                self.assertTrue(
                    imported_names,
                    f"{relative_path} does not import execute_agent.",
                )
                self.assertTrue(
                    calls,
                    f"{relative_path} does not call execute_agent.",
                )


def _project_python_sources() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.py")
        if not EXCLUDED_SOURCE_DIRECTORIES.intersection(
            path.relative_to(ROOT).parts
        )
    )


def _direct_runner_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(
        path.read_text(encoding="utf-8-sig"),
        filename=str(path),
    )
    runner_names: set[str] = set()
    agents_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "agents":
            runner_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "Runner"
            )
        elif isinstance(node, ast.Import):
            agents_module_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "agents"
            )

    calls: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(
            node.func,
            ast.Attribute,
        ):
            continue
        method_name = node.func.attr
        if method_name not in RUNNER_EXECUTION_METHODS:
            continue
        target = node.func.value
        direct_runner = (
            isinstance(target, ast.Name) and target.id in runner_names
        )
        module_runner = (
            isinstance(target, ast.Attribute)
            and target.attr == "Runner"
            and isinstance(target.value, ast.Name)
            and target.value.id in agents_module_names
        )
        if direct_runner or module_runner:
            calls.append((node.lineno, method_name))
    return sorted(calls)


if __name__ == "__main__":
    unittest.main()
