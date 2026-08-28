import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from council.context import (
    ContextBuilderError,
    _list_tracked_files,
    _resolve_tracked_path,
    build_context,
    derive_search_terms,
    validate_repository_evidence_ids,
)
from council.models import ContextBuilderConfig


class ContextBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name) / "example-repository"
        self.repository.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "example@example.invalid")
        self._git("config", "user.name", "Example User")
        self._write("README.md", "# Example Repository\n")
        self._write(
            "src/FeatureService.py",
            "class ExampleSystem:\n    reward_enabled = True\n",
        )
        self._write("config/RewardConfig.json", '{"reward": true}\n')
        self._commit_all("initial content")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_valid_repository_identity_and_tracked_files(self) -> None:
        context = build_context(self.repository, "Example reward feature")

        self.assertEqual(context.repository_path, str(self.repository.resolve()))
        self.assertEqual(context.commit_sha, self._git("rev-parse", "HEAD"))
        self.assertEqual(context.branch, "main")
        self.assertFalse(context.working_tree_dirty)
        self.assertEqual(context.tracked_file_count, 3)
        self.assertEqual(
            _list_tracked_files(self.repository),
            [
                "config/RewardConfig.json",
                "README.md",
                "src/FeatureService.py",
            ],
        )

    def test_non_git_directory_is_rejected(self) -> None:
        non_repository = Path(self.temporary_directory.name) / "not-a-repository"
        non_repository.mkdir()

        with self.assertRaisesRegex(
            ContextBuilderError,
            "Path is not inside a Git working tree",
        ):
            build_context(non_repository, "Example feature")

    def test_search_terms_are_normalized_deduplicated_and_bounded(self) -> None:
        feature_input = (
            "The Repeat, repeat-engagement experiment for UI and rewards."
        )

        self.assertEqual(
            derive_search_terms(feature_input, max_terms=4),
            ["repeat", "engagement", "experiment", "reward"],
        )

    def test_structured_brief_labels_do_not_starve_later_terms(self) -> None:
        feature_input = """Feature Name: Weekly concept
Player Product Problem: Players need a reason to return.
Feature Idea: Early description.

Implementation Questions:
Progression eligibility persistence analytics economy.
Progression eligibility persistence analytics economy participation.
"""

        terms = derive_search_terms(feature_input)

        for label in ("feature", "name", "product", "problem", "players"):
            self.assertNotIn(label, terms)
        for meaningful_term in (
            "progression",
            "eligibility",
            "persistence",
            "analytics",
            "economy",
            "participation",
        ):
            self.assertIn(meaningful_term, terms)
        self.assertLessEqual(len(terms), 12)

    def test_simple_singular_plural_variants_share_one_term(self) -> None:
        terms = derive_search_terms(
            "reward rewards opportunity opportunities "
            "system systems event events",
        )

        self.assertCountEqual(
            terms,
            ["reward", "opportunity", "system", "event"],
        )
        self.assertEqual(len(terms), 4)

    def test_normalized_term_searches_known_plural_forms(self) -> None:
        self._write(
            "src/OpportunityService.py",
            "future opportunities remain available\n",
        )
        self._commit_all("add plural-only implementation wording")

        context = build_context(self.repository, "opportunities")

        selected = next(
            item
            for item in context.evidence
            if item.file_path == "src/OpportunityService.py"
        )
        self.assertEqual(context.search_terms, ["opportunity"])
        self.assertEqual(selected.matched_terms, ["opportunity"])

    def test_detached_head_has_no_branch(self) -> None:
        self._git("checkout", "--detach")

        context = build_context(self.repository, "Example feature")

        self.assertIsNone(context.branch)
        self.assertEqual(context.commit_sha, self._git("rev-parse", "HEAD"))

    def test_relevant_selection_and_order_are_deterministic(self) -> None:
        first = build_context(self.repository, "Example reward feature")
        second = build_context(self.repository, "Example reward feature")

        first_paths = [item.file_path for item in first.evidence]
        second_paths = [item.file_path for item in second.evidence]
        self.assertEqual(first.search_terms, second.search_terms)
        self.assertEqual(first_paths, second_paths)
        self.assertEqual(
            [item.id for item in first.evidence],
            [item.id for item in second.evidence],
        )
        self.assertIn("src/FeatureService.py", first_paths)
        selected = next(
            item
            for item in first.evidence
            if item.file_path == "src/FeatureService.py"
        )
        self.assertIn("feature term match", selected.selection_reasons)
        self.assertEqual(selected.matched_terms, ["example", "reward"])

    def test_core_project_context_is_not_starved_by_feature_matches(self) -> None:
        self._write(
            "AGENTS.md",
            "Repository instructions that do not contain the search term.\n",
        )
        for index in range(12):
            self._write(
                f"src/Matching{index:02d}.py",
                "crowdedterm = True\n",
            )
        self._commit_all("add instructions and matching sources")
        configuration = ContextBuilderConfig(max_selected_files=5)

        context = build_context(self.repository, "crowdedterm", configuration)

        self.assertIn("AGENTS.md", [item.file_path for item in context.evidence])
        self.assertEqual(context.evidence[0].file_path, "AGENTS.md")
        self.assertIn(
            "reserved core context",
            context.evidence[0].selection_reasons,
        )

    def test_meta_files_do_not_outrank_relevant_text_files(self) -> None:
        self._write(
            "Assets/EligibilityService.cs.meta",
            "reward eligibility reward eligibility name name\n",
        )
        self._write(
            "src/EligibilityService.py",
            "reward eligibility implementation\n",
        )
        self._commit_all("add metadata noise and implementation")
        configuration = ContextBuilderConfig(max_selected_files=3)

        context = build_context(
            self.repository,
            "reward eligibility",
            configuration,
        )
        selected_paths = [item.file_path for item in context.evidence]

        self.assertIn("src/EligibilityService.py", selected_paths)
        self.assertNotIn("Assets/EligibilityService.cs.meta", selected_paths)

    def test_root_readme_is_reserved_without_nested_readme_boilerplate(self) -> None:
        self._write(
            "docs/Readme/Scripts/ReadmeEditor.py",
            "Feature Name Product Problem Players Need\n",
        )
        self._write("src/Progression.py", "progression eligibility\n")
        self._write("config/Eligibility.yaml", "progression: eligibility\n")
        self._commit_all("add nested readme boilerplate")
        configuration = ContextBuilderConfig(max_selected_files=4)

        context = build_context(
            self.repository,
            "Feature Name: progression eligibility",
            configuration,
        )
        selected_paths = [item.file_path for item in context.evidence]

        self.assertIn("README.md", selected_paths)
        root_readme = next(
            item for item in context.evidence if item.file_path == "README.md"
        )
        self.assertIn("reserved core context", root_readme.selection_reasons)
        self.assertNotIn(
            "docs/Readme/Scripts/ReadmeEditor.py",
            selected_paths,
        )

    def test_strong_implementation_matches_survive_bounded_selection(self) -> None:
        for index in range(8):
            self._write(
                f"docs/Note{index:02d}.md",
                "reward overview\n",
            )
        self._write(
            "src/ProgressionEligibility.py",
            "reward eligibility progression\n",
        )
        self._write(
            "config/Participation.yaml",
            "reward: eligibility\nprogression: enabled\n",
        )
        self._commit_all("add crowded implementation evidence")
        configuration = ContextBuilderConfig(max_selected_files=3)

        context = build_context(
            self.repository,
            "reward eligibility progression",
            configuration,
        )
        selected_paths = [item.file_path for item in context.evidence]

        self.assertIn("src/ProgressionEligibility.py", selected_paths)
        self.assertIn("config/Participation.yaml", selected_paths)

    def test_per_file_text_is_truncated(self) -> None:
        self._write("src/LargeSource.py", "unique needle " + ("x" * 100))
        self._commit_all("add large source")
        configuration = ContextBuilderConfig(
            max_selected_files=2,
            max_characters_per_file=10,
            max_total_characters=100,
        )

        context = build_context(
            self.repository,
            "unique needle",
            configuration,
        )

        selected = next(
            item
            for item in context.evidence
            if item.file_path == "src/LargeSource.py"
        )
        self.assertEqual(len(selected.text), 10)
        self.assertTrue(selected.truncated)
        self.assertEqual(context.truncated_file_count, 2)

    def test_total_text_bound_is_enforced(self) -> None:
        self._write("src/Alpha.py", "boundterm " + ("a" * 100))
        self._write("src/Beta.py", "boundterm " + ("b" * 100))
        self._commit_all("add bounded sources")
        configuration = ContextBuilderConfig(
            max_selected_files=5,
            max_characters_per_file=80,
            max_total_characters=100,
        )

        context = build_context(self.repository, "boundterm", configuration)

        self.assertEqual(context.total_text_characters, 100)
        self.assertLessEqual(
            sum(len(item.text) for item in context.evidence),
            configuration.max_total_characters,
        )
        self.assertTrue(any(item.truncated for item in context.evidence))

    def test_binary_file_is_skipped_safely(self) -> None:
        self._write_bytes("src/Binary.py", b"binaryterm\0payload")
        self._write("src/Text.py", "binaryterm readable\n")
        self._commit_all("add binary and text files")

        context = build_context(self.repository, "binaryterm")

        self.assertNotIn(
            "src/Binary.py",
            [item.file_path for item in context.evidence],
        )
        self.assertIn(
            "src/Text.py",
            [item.file_path for item in context.evidence],
        )
        self.assertGreaterEqual(context.skipped_file_count, 1)

    def test_evidence_ids_and_validator_are_deterministic(self) -> None:
        context = build_context(self.repository, "Example reward feature")
        evidence_ids = [item.id for item in context.evidence]

        self.assertEqual(
            evidence_ids,
            [f"repo-{index:03d}" for index in range(1, len(evidence_ids) + 1)],
        )
        selected = validate_repository_evidence_ids(
            [evidence_ids[0], evidence_ids[-1]],
            context,
        )
        self.assertEqual(
            [item.id for item in selected],
            [evidence_ids[0], evidence_ids[-1]],
        )

        with self.assertRaisesRegex(
            ContextBuilderError,
            "Unknown repository evidence IDs: repo-999",
        ):
            validate_repository_evidence_ids(["repo-999"], context)

        with self.assertRaisesRegex(
            ContextBuilderError,
            "Unknown repository evidence IDs: agent_inference",
        ):
            validate_repository_evidence_ids(["agent_inference"], context)

    def test_paths_outside_repository_are_rejected(self) -> None:
        self.assertIsNone(_resolve_tracked_path(self.repository, "../outside.py"))
        self.assertIsNone(_resolve_tracked_path(self.repository, "/outside.py"))

        with patch(
            "council.context._list_tracked_files",
            return_value=["../outside.py"],
        ), patch(
            "council.context._search_feature_terms",
            return_value={"../outside.py": {"outside"}},
        ):
            context = build_context(self.repository, "outside")

        self.assertEqual(context.evidence, [])
        self.assertEqual(context.skipped_file_count, 1)

    def test_context_build_does_not_modify_working_tree_contents(self) -> None:
        before = self._working_tree_contents()
        before_status = self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        )

        build_context(self.repository, "Example reward feature")

        self.assertEqual(self._working_tree_contents(), before)
        self.assertEqual(
            self._git(
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
            ),
            before_status,
        )

    def test_modified_tracked_content_is_read_and_marked_dirty(self) -> None:
        head_sha = self._git("rev-parse", "HEAD")
        current_content = "workingtree marker from current content\n"
        self._write("src/FeatureService.py", current_content)

        context = build_context(self.repository, "workingtree marker")

        self.assertEqual(context.commit_sha, head_sha)
        self.assertTrue(context.working_tree_dirty)
        selected = next(
            item
            for item in context.evidence
            if item.file_path == "src/FeatureService.py"
        )
        self.assertEqual(selected.text, current_content)
        self.assertEqual(selected.matched_terms, ["workingtree", "marker"])

    def test_untracked_files_are_not_selected_or_marked_dirty(self) -> None:
        self._write(
            "src/UntrackedFeature.py",
            "secretuntracked content must not be selected\n",
        )

        context = build_context(self.repository, "secretuntracked")

        self.assertFalse(context.working_tree_dirty)
        self.assertEqual(context.tracked_file_count, 3)
        self.assertNotIn(
            "src/UntrackedFeature.py",
            [item.file_path for item in context.evidence],
        )

    def test_dubious_ownership_error_preserves_git_diagnostics(self) -> None:
        git_error = (
            "fatal: detected dubious ownership in repository\n"
            "configure safe.directory to trust it"
        )
        completed_process = subprocess.CompletedProcess(
            args=["git"],
            returncode=128,
            stdout="",
            stderr=git_error,
        )

        with patch(
            "council.context.subprocess.run",
            return_value=completed_process,
        ):
            with self.assertRaises(ContextBuilderError) as raised:
                build_context(self.repository, "Example feature")

        message = str(raised.exception)
        self.assertIn("ownership or trust configuration", message)
        self.assertIn("safe.directory", message)
        self.assertIn(git_error, message)

    def _write(self, relative_path: str, content: str) -> None:
        path = self.repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_bytes(self, relative_path: str, content: bytes) -> None:
        path = self.repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            capture_output=True,
            check=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    def _commit_all(self, message: str) -> None:
        self._git("add", "--all")
        self._git("commit", "-m", message)

    def _working_tree_contents(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.repository).as_posix(): path.read_bytes()
            for path in self.repository.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }


if __name__ == "__main__":
    unittest.main()
