"""Deterministic concern collection and bounded resolver repository lookup."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from council.context import (
    ContextBuilderError,
    _list_tracked_files,
    _read_bounded_text,
    _resolve_tracked_path,
    _run_git,
    _validate_repository,
)
from council.models import (
    AnalyticsResult,
    ConcernKind,
    EvidenceLookupRequest,
    GameDesignResult,
    ScopeRiskResult,
    SourceConcern,
    SpecialistCommon,
    SupplementalRepositoryEvidence,
    TechnicalResult,
)


MAX_LOOKUP_REQUESTS = 3
MAX_LOOKUP_TERMS_PER_REQUEST = 5
MAX_SUPPLEMENTAL_FILES = 4
MAX_SUPPLEMENTAL_CHARACTERS = 12_000
MAX_SUPPLEMENTAL_CHARACTERS_PER_FILE = 4_000


def feature_sha256(feature: str) -> str:
    return hashlib.sha256(feature.encode("utf-8")).hexdigest()


def collect_source_concerns(
    game_design: GameDesignResult,
    technical: TechnicalResult,
    analytics: AnalyticsResult,
    scope_risk: ScopeRiskResult,
) -> list[SourceConcern]:
    """Collect all report-facing specialist concerns with stable source IDs."""
    collected: list[SourceConcern] = []
    specialists: tuple[tuple[str, SpecialistCommon, tuple[tuple[ConcernKind, Iterable[str]], ...]], ...] = (
        (
            "game_design",
            game_design,
            ((ConcernKind.RISK, game_design.risks), (ConcernKind.UNKNOWN, game_design.unknowns)),
        ),
        (
            "technical",
            technical,
            (
                (ConcernKind.RISK, technical.risks),
                (ConcernKind.UNKNOWN, technical.unknowns),
            ),
        ),
        (
            "analytics",
            analytics,
            ((ConcernKind.RISK, analytics.risks), (ConcernKind.UNKNOWN, analytics.unknowns)),
        ),
        (
            "scope_risk",
            scope_risk,
            (
                (ConcernKind.RISK, scope_risk.risks),
                (ConcernKind.UNKNOWN, scope_risk.unknowns),
            ),
        ),
    )
    for role, specialist, groups in specialists:
        index = 0
        for kind, texts in groups:
            for text in texts:
                if not text.strip():
                    continue
                index += 1
                collected.append(
                    SourceConcern(
                        id=f"{role.replace('_', '-')}-{index:03d}",
                        source_role=role,  # type: ignore[arg-type]
                        source_index=index,
                        kind=kind,
                        text=text,
                        # Existing specialist schemas do not associate their
                        # text-only risks/unknowns with individual evidence.
                        # Do not invent provenance by attaching all evidence.
                        evidence_ids=[],
                    )
                )
    return collected


def bounded_targeted_lookup(
    repository_path: str,
    requests: Iterable[EvidenceLookupRequest],
) -> tuple[list[SupplementalRepositoryEvidence], list[str]]:
    """Search current tracked working-tree files with fixed-string Git searches.

    Requests are model output, so this deliberately accepts only bounded plain
    terms and never constructs shell commands or regular expressions.
    """
    request_list = list(requests)[:MAX_LOOKUP_REQUESTS]
    terms: list[str] = []
    for request in request_list:
        for term in request.search_terms[:MAX_LOOKUP_TERMS_PER_REQUEST]:
            value = term.strip()
            if value and value not in terms:
                terms.append(value)
    if not terms:
        return [], ["No non-empty bounded repository lookup terms were supplied."]

    repository = _validate_repository(repository_path)
    tracked_files = set(_list_tracked_files(repository))
    matches: dict[str, set[str]] = {}
    for term in terms:
        output = _run_git(
            repository,
            "grep", "-l", "-i", "-F", "-z", "-e", term, "--",
            allowed_return_codes=(0, 1),
        )
        for path in (value for value in output.split("\0") if value):
            if path in tracked_files:
                matches.setdefault(path, set()).add(term)

    candidates = sorted(
        matches,
        key=lambda path: (-len(matches[path]), path.casefold(), path),
    )
    evidence: list[SupplementalRepositoryEvidence] = []
    limitations: list[str] = []
    characters = 0
    for path in candidates:
        if len(evidence) >= MAX_SUPPLEMENTAL_FILES:
            limitations.append("Supplemental file limit reached.")
            break
        remaining = MAX_SUPPLEMENTAL_CHARACTERS - characters
        if remaining <= 0:
            limitations.append("Supplemental character limit reached.")
            break
        resolved = _resolve_tracked_path(repository, path)
        if resolved is None:
            limitations.append(f"Skipped unsafe or unavailable tracked path: {path}")
            continue
        excerpt = _read_bounded_text(
            resolved,
            min(MAX_SUPPLEMENTAL_CHARACTERS_PER_FILE, remaining),
        )
        if excerpt is None:
            limitations.append(f"Skipped binary or invalid UTF-8 tracked file: {path}")
            continue
        text, truncated = excerpt
        evidence.append(
            SupplementalRepositoryEvidence(
                id=f"resolver-repo-{len(evidence) + 1:03d}",
                file_path=path,
                matched_terms=sorted(matches[path], key=lambda value: (value.casefold(), value)),
                text=text,
                truncated=truncated,
            )
        )
        characters += len(text)
    if not evidence and not limitations:
        limitations.append("No tracked repository files matched the bounded lookup terms.")
    return evidence, limitations
