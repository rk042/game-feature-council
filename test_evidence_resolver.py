import subprocess
import tempfile
import unittest
from pathlib import Path

from council.evidence_resolver import (
    MAX_SUPPLEMENTAL_CHARACTERS,
    MAX_SUPPLEMENTAL_FILES,
    bounded_targeted_lookup,
    collect_source_concerns,
)
from council.models import (
    ConcernKind,
    EvidenceLookupRequest,
    EvidenceResolverResult,
    ResolvedConcern,
    ResolutionStatus,
)
from test_producer_agent import ANALYTICS, GAME_DESIGN, SCOPE_RISK, TECHNICAL


class EvidenceResolverTests(unittest.TestCase):
    def test_collects_report_facing_concerns_with_stable_provenance(self) -> None:
        game_design = GAME_DESIGN.model_copy(
            update={"risks": ["Design risk"], "unknowns": ["Design unknown"]}
        )
        technical = TECHNICAL.model_copy(
            update={"risks": ["Technical risk"], "unknowns": ["Technical unknown"]}
        )
        analytics = ANALYTICS.model_copy(
            update={"risks": ["Analytics risk"], "unknowns": []}
        )
        scope_risk = SCOPE_RISK.model_copy(
            update={"risks": [], "unknowns": ["Scope unknown"]}
        )

        concerns = collect_source_concerns(
            game_design, technical, analytics, scope_risk
        )

        self.assertEqual(
            [(item.id, item.kind, item.text) for item in concerns],
            [
                ("game-design-001", ConcernKind.RISK, "Design risk"),
                ("game-design-002", ConcernKind.UNKNOWN, "Design unknown"),
                ("technical-001", ConcernKind.RISK, "Technical risk"),
                ("technical-002", ConcernKind.UNKNOWN, "Technical unknown"),
                ("analytics-001", ConcernKind.RISK, "Analytics risk"),
                ("scope-risk-001", ConcernKind.UNKNOWN, "Scope unknown"),
            ],
        )
        self.assertTrue(all(not item.evidence_ids for item in concerns))

    def test_resolution_status_contract_prevents_fake_human_questions(self) -> None:
        with self.assertRaises(ValueError):
            ResolvedConcern(
                concern_id="concern-001",
                kind="unknown",
                canonical_concern="Question",
                source_concern_ids=["technical-001"],
                status=ResolutionStatus.RESOLVED_FROM_REPOSITORY,
                resolution="Answer without evidence",
            )
        with self.assertRaises(ValueError):
            ResolvedConcern(
                concern_id="concern-002",
                kind="unknown",
                canonical_concern="Frequency",
                source_concern_ids=["analytics-001"],
                status=ResolutionStatus.REQUIRES_EXPERIMENT,
                why_unresolved="Depends on gameplay.",
                how_to_answer="Measure it.",
                human_question="How frequent is it?",
            )

    def test_bounded_lookup_uses_tracked_working_tree_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            self._git(repository, "init")
            self._git(repository, "config", "user.email", "tests@example.invalid")
            self._git(repository, "config", "user.name", "Tests")
            (repository / "tracked.txt").write_text(
                "Current target behaviour\n", encoding="utf-8"
            )
            self._git(repository, "add", "tracked.txt")
            self._git(repository, "commit", "-m", "initial")
            # A tracked modification must be read from the current working tree.
            (repository / "tracked.txt").write_text(
                "Current target behaviour changed\n", encoding="utf-8"
            )
            (repository / "untracked.txt").write_text(
                "Current target behaviour secret\n", encoding="utf-8"
            )

            evidence, limitations = bounded_targeted_lookup(
                str(repository),
                [
                    EvidenceLookupRequest(
                        concern_ids=["technical-001"],
                        search_terms=["target behaviour"],
                    )
                ],
            )

        self.assertEqual([item.id for item in evidence], ["resolver-repo-001"])
        self.assertEqual([item.file_path for item in evidence], ["tracked.txt"])
        self.assertIn("changed", evidence[0].text)
        self.assertNotIn("secret", evidence[0].text)
        self.assertLessEqual(len(evidence), MAX_SUPPLEMENTAL_FILES)
        self.assertLessEqual(sum(len(item.text) for item in evidence), MAX_SUPPLEMENTAL_CHARACTERS)
        self.assertIsInstance(limitations, list)

    def test_empty_concerns_and_no_lookup_are_valid(self) -> None:
        self.assertEqual(
            collect_source_concerns(
                GAME_DESIGN.model_copy(update={"risks": [], "unknowns": []}),
                TECHNICAL.model_copy(update={"risks": [], "unknowns": []}),
                ANALYTICS.model_copy(update={"risks": [], "unknowns": []}),
                SCOPE_RISK.model_copy(update={"risks": [], "unknowns": []}),
            ),
            [],
        )
        self.assertEqual(EvidenceResolverResult().lookup_requests, [])

    @staticmethod
    def _git(repository: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

