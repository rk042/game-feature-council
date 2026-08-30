import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from rich.console import Console
from typer.testing import CliRunner

from council.cli import (
    MODE_BOTH,
    MODE_GENERALIST,
    PlainOutput,
    RunCompletion,
    _interactive_wizard,
    _prompt_for_feature,
    _prompt_for_post_run_action,
    _prompt_for_repository,
    app,
    load_recent_repositories,
    remember_repository,
)
from council.context import ContextBuilderError
from council.models import TokenUsage
from council.presentation import ExecutionProgress
from council.progress import ProgressStatus, make_progress_event
import test_cli
from test_producer_agent import FEATURE


class Phase2CliTests(unittest.TestCase):
    def test_root_no_command_launches_wizard_only_when_interactive(self) -> None:
        runner = CliRunner()
        with (
            patch("council.cli._is_interactive_terminal", return_value=True),
            patch("council.cli._interactive_wizard", return_value=0) as wizard,
        ):
            interactive = runner.invoke(app, [])

        self.assertEqual(interactive.exit_code, 0, interactive.output)
        wizard.assert_called_once()

        with (
            patch("council.cli._is_interactive_terminal", return_value=False),
            patch("council.cli._interactive_wizard") as wizard,
        ):
            noninteractive = runner.invoke(app, [])

        self.assertEqual(noninteractive.exit_code, 2)
        self.assertIn("No command supplied", noninteractive.output)
        wizard.assert_not_called()

    def test_repository_prompt_normalizes_quotes_and_retries_invalid(self) -> None:
        answers = iter(["", '"C:\\bad"', '"D:\\valid"'])
        output: list[str] = []
        with (
            patch("council.cli.load_recent_repositories", return_value=[]),
            patch(
                "council.cli.validate_repository_path",
                side_effect=[
                    ContextBuilderError("Not a valid Git repository."),
                    Path("D:/valid"),
                ],
            ) as validate,
        ):
            selected = _prompt_for_repository(
                lambda _prompt: next(answers),
                PlainOutput(output.append),
            )

        self.assertEqual(selected, str(Path("D:/valid")))
        self.assertEqual(
            [call.args[0] for call in validate.call_args_list],
            ["C:\\bad", "D:\\valid"],
        )
        self.assertIn("try again", "\n".join(output))
        self.assertIn("Repository path is required", "\n".join(output))

    def test_repository_prompt_accepts_recent_selection(self) -> None:
        recent = "D:/recent/repository"
        with (
            patch(
                "council.cli.load_recent_repositories",
                return_value=[recent],
            ),
            patch(
                "council.cli.validate_repository_path",
                return_value=Path(recent),
            ) as validate,
        ):
            selected = _prompt_for_repository(
                lambda _prompt: "1",
                PlainOutput(lambda _text: None),
            )
        self.assertEqual(selected, str(Path(recent)))
        validate.assert_called_once_with(recent)

    def test_wizard_feature_file_preserves_raw_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "feature.txt"
            raw = "  First line\nSecond line\n"
            path.write_text(raw, encoding="utf-8")
            answers = iter(["yes", f'"{path}"'])
            feature, feature_file = _prompt_for_feature(
                lambda _prompt: next(answers),
                PlainOutput(lambda _text: None),
            )
        self.assertEqual(feature, raw)
        self.assertIsNone(feature_file)

    def test_wizard_decline_runs_preflight_and_zero_models(self) -> None:
        helper = test_cli.CliTests()
        context = helper._context("D:/synthetic/target")
        answers = iter(
            [
                "D:/synthetic/target",
                "n",
                "  Exact first line  ",
                "Second line",
                "",
                "",
                "0.10",
                "",
                "",
            ]
        )
        output: list[str] = []
        council = AsyncMock()
        generalist = AsyncMock()
        with (
            patch.dict(os.environ, test_cli.MODEL_ENVIRONMENT, clear=False),
            patch("council.cli.load_recent_repositories", return_value=[]),
            patch(
                "council.cli.validate_repository_path",
                return_value=Path(context.repository_path),
            ),
            patch("council.cli.build_context", return_value=context) as build,
            patch("council.cli.run_council_with_telemetry", council),
            patch("council.cli.run_generalist", generalist),
        ):
            from council.cli import _run_with_error_boundary

            plain = PlainOutput(output.append)
            code = _run_with_error_boundary(
                "run",
                lambda: _interactive_wizard(
                    input_fn=lambda _prompt: next(answers),
                    output=plain,
                ),
                plain,
            )

        self.assertEqual(code, 2)
        build.assert_called_once_with(
            str(Path(context.repository_path)),
            "  Exact first line  \nSecond line",
        )
        council.assert_not_awaited()
        generalist.assert_not_awaited()
        rendered = "\n".join(output)
        self.assertIn("Preflight estimate", rendered)
        self.assertIn("consent was not granted", rendered)

    def test_wizard_accept_uses_mocked_generalist_then_exit(self) -> None:
        helper = test_cli.CliTests()
        context = helper._context("D:/synthetic/target")
        execution = helper._generalist_execution(context)
        answers = iter(
            [
                context.repository_path,
                "n",
                FEATURE,
                "",
                "3",
                "0.10",
                "n",
                "yes",
                "3",
            ]
        )
        output: list[str] = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_directory = root / "run-id"
            report = run_directory / "report.md"
            generalist = AsyncMock(return_value=execution)
            with (
                patch.dict(os.environ, test_cli.MODEL_ENVIRONMENT, clear=False),
                patch("council.cli.load_recent_repositories", return_value=[]),
                patch(
                    "council.cli.validate_repository_path",
                    return_value=Path(context.repository_path),
                ),
                patch("council.cli.build_context", return_value=context) as build,
                patch("council.cli.run_council_with_telemetry") as council,
                patch("council.cli.run_generalist", generalist),
                patch("council.cli._prepare_output_root", return_value=root),
                patch(
                    "council.cli._write_mode_artifacts",
                    return_value=("run-id", run_directory, report),
                ),
                patch("council.cli.remember_repository") as remember,
            ):
                code = _interactive_wizard(
                    input_fn=lambda _prompt: next(answers),
                    output=PlainOutput(output.append),
                )

        self.assertEqual(code, 0)
        build.assert_called_once_with(str(Path(context.repository_path)), FEATURE)
        council.assert_not_called()
        generalist.assert_awaited_once()
        supplied_feature, supplied_context = generalist.await_args.args
        self.assertEqual(supplied_feature, FEATURE)
        self.assertIs(supplied_context, context)
        self.assertIn("progress_listener", generalist.await_args.kwargs)
        remember.assert_called_once_with(str(Path(context.repository_path)))
        rendered = "\n".join(output)
        self.assertIn("Generalist decision", rendered)
        self.assertIn(str(report), rendered)

    def test_recent_repository_store_is_bounded_deduplicated_and_path_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = root / "config" / "recent.json"
            repositories = [root / f"repo-{index}" for index in range(7)]
            for repository in repositories:
                repository.mkdir()

            with (
                patch("council.cli._recent_repository_store_path", return_value=store),
                patch(
                    "council.cli.validate_repository_path",
                    side_effect=lambda value: Path(value).resolve(),
                ),
            ):
                for repository in repositories:
                    remember_repository(str(repository))
                remember_repository(str(repositories[-1]))
                loaded = load_recent_repositories()

            self.assertEqual(len(loaded), 5)
            self.assertEqual(loaded[0], str(repositories[-1].resolve()))
            payload = json.loads(store.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {"repositories"})
            serialized = store.read_text(encoding="utf-8")
            self.assertNotIn("feature", serialized.casefold())
            self.assertNotIn("api_key", serialized.casefold())

    def test_malformed_recent_history_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = Path(temporary_directory) / "recent.json"
            store.write_text("{not valid json", encoding="utf-8")
            with patch(
                "council.cli._recent_repository_store_path",
                return_value=store,
            ):
                self.assertEqual(load_recent_repositories(), [])

    def test_recent_history_never_writes_inside_analyzed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory).resolve()
            store = repository / ".local" / "recent.json"
            with patch(
                "council.cli._recent_repository_store_path",
                return_value=store,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "inside the analyzed repository",
                ):
                    remember_repository(str(repository))
            self.assertFalse(store.exists())

    def test_config_command_clears_recent_history(self) -> None:
        runner = CliRunner()
        with patch(
            "council.cli.clear_recent_repositories",
            return_value=True,
        ) as clear:
            result = runner.invoke(app, ["config", "clear-recent"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("history cleared", result.output)
        clear.assert_called_once_with()

    def test_post_run_review_reuses_review_command(self) -> None:
        completion = RunCompletion(
            run_id="run-id",
            run_directory=Path("D:/runs/run-id"),
            report_path=Path("D:/runs/run-id/report.md"),
        )
        with patch("council.cli._review_command", return_value=0) as review:
            _prompt_for_post_run_action(
                completion,
                MODE_BOTH,
                input_fn=lambda _prompt: "1",
                output=PlainOutput(lambda _text: None),
            )
        review.assert_called_once()
        self.assertEqual(review.call_args.args[0].run, "run-id")

    def test_report_open_failure_does_not_invalidate_completed_run(self) -> None:
        completion = RunCompletion(
            run_id="run-id",
            run_directory=Path("D:/runs/run-id"),
            report_path=Path("D:/runs/run-id/report.html"),
        )
        output: list[str] = []
        with patch("council.cli.open_report", side_effect=OSError("no shell")):
            _prompt_for_post_run_action(
                completion,
                MODE_GENERALIST,
                input_fn=lambda _prompt: "2",
                output=PlainOutput(output.append),
            )
        self.assertIn("Unable to open the report", "\n".join(output))
        self.assertIn(str(completion.report_path), "\n".join(output))

    def test_plain_and_live_progress_expose_semantic_stage_state(self) -> None:
        lines: list[str] = []
        plain = ExecutionProgress(
            MODE_GENERALIST,
            print_fn=lines.append,
            live=False,
        )
        plain.complete_context(12, 2, 100)
        plain(
            make_progress_event(
                "generalist",
                ProgressStatus.STARTED,
                model="gpt-5.4-nano",
            )
        )
        plain(
            make_progress_event(
                "generalist",
                ProgressStatus.COMPLETED,
                duration_ms=20,
                model="gpt-5.4-nano",
                usage=TokenUsage(requests=1, total_tokens=30),
            )
        )
        self.assertIn("Generalist baseline", "\n".join(lines))
        self.assertIn("Completed", "\n".join(lines))

        stream = io.StringIO()
        console = Console(file=stream, force_terminal=False, width=100)
        live = ExecutionProgress(
            MODE_GENERALIST,
            print_fn=lambda _text: None,
            console=console,
            live=True,
        )
        live.start()
        live.complete_context(10, 1, 50)
        live(
            make_progress_event(
                "generalist",
                ProgressStatus.STARTED,
            )
        )
        live.stop()
        rendered = stream.getvalue()
        self.assertIn("Context Builder", rendered)
        self.assertIn("Generalist baseline", rendered)
        self.assertIn("Running", rendered)


if __name__ == "__main__":
    unittest.main()
