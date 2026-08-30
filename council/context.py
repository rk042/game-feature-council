import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Iterable

from council.models import (
    ContextBuilderConfig,
    ContextBundle,
    RepositoryEvidence,
)


DEFAULT_CONFIG = ContextBuilderConfig()
CORE_CONTEXT_FILE_LIMIT = 3

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
_STOP_WORDS = frozenset(
    {
        "a",
        "already",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "can",
        "cannot",
        "by",
        "could",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "if",
        "in",
        "is",
        "it",
        "may",
        "meaningful",
        "might",
        "more",
        "must",
        "no",
        "not",
        "of",
        "on",
        "only",
        "or",
        "should",
        "that",
        "the",
        "their",
        "them",
        "then",
        "they",
        "this",
        "through",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "who",
        "why",
        "will",
        "with",
        "without",
        "would",
    }
)
_FEATURE_BRIEF_STOP_WORDS = frozenset(
    {
        "constraint",
        "constraints",
        "decision",
        "design",
        "feature",
        "goal",
        "goals",
        "idea",
        "important",
        "name",
        "need",
        "needs",
        "open",
        "player",
        "players",
        "problem",
        "product",
        "question",
        "questions",
        "threshold",
        "thresholds",
    }
)
_TERM_VARIANTS = {
    "events": "event",
    "goals": "goal",
    "needs": "need",
    "opportunities": "opportunity",
    "players": "player",
    "questions": "question",
    "rewards": "reward",
    "systems": "system",
    "thresholds": "threshold",
}
_TERM_SEARCH_FORMS = {
    "event": ("event", "events"),
    "goal": ("goal", "goals"),
    "need": ("need", "needs"),
    "opportunity": ("opportunity", "opportunities"),
    "player": ("player", "players"),
    "question": ("question", "questions"),
    "reward": ("reward", "rewards"),
    "system": ("system", "systems"),
    "threshold": ("threshold", "thresholds"),
}
_MANIFEST_NAMES = frozenset(
    {
        "cargo.toml",
        "composer.json",
        "directory.build.props",
        "go.mod",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
    }
)
_MANIFEST_SUFFIXES = frozenset(
    {
        ".asmdef",
        ".csproj",
        ".fsproj",
        ".sln",
        ".vbproj",
    }
)
_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".dart",
        ".fs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".swift",
        ".ts",
        ".tsx",
    }
)
_CONFIG_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".ini",
        ".json",
        ".properties",
        ".props",
        ".targets",
        ".toml",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_DOCUMENTATION_SUFFIXES = frozenset({".adoc", ".md", ".rst", ".txt"})


class ContextBuilderError(RuntimeError):
    pass


@dataclass(frozen=True)
class _RankedCandidate:
    file_path: str
    selection_reasons: tuple[str, ...]
    matched_terms: tuple[str, ...]
    score: int


def derive_search_terms(
    feature_input: str,
    max_terms: int = DEFAULT_CONFIG.max_search_terms,
) -> list[str]:
    term_stats: dict[str, tuple[int, set[int], int]] = {}
    section_index = 0
    token_index = 0

    for line in feature_input.splitlines():
        if not line.strip():
            section_index += 1
            continue

        for match in _TOKEN_PATTERN.finditer(line):
            raw_term = match.group(0).casefold()
            term = _TERM_VARIANTS.get(raw_term, raw_term)
            if (
                term in _STOP_WORDS
                or term in _FEATURE_BRIEF_STOP_WORDS
                or len(term) < 2
            ):
                token_index += 1
                continue

            count, sections, first_appearance = term_stats.get(
                term,
                (0, set(), token_index),
            )
            sections.add(section_index)
            term_stats[term] = (count + 1, sections, first_appearance)
            token_index += 1

    ranked_terms = sorted(
        term_stats,
        key=lambda term: (
            -term_stats[term][0],
            -len(term_stats[term][1]),
            -len(term),
            term_stats[term][2],
            term,
        ),
    )
    return ranked_terms[:max_terms]


def build_context(
    repository_path: str | Path,
    feature_input: str,
    configuration: ContextBuilderConfig | None = None,
) -> ContextBundle:
    config = configuration or ContextBuilderConfig()
    repository = _validate_repository(repository_path)
    commit_sha = _run_git(repository, "rev-parse", "HEAD").strip()
    branch_output = _run_git(repository, "branch", "--show-current").strip()
    branch = branch_output or None
    working_tree_dirty = bool(
        _run_git(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            "--ignore-submodules=untracked",
            "-z",
        )
    )

    tracked_files = _list_tracked_files(repository)
    search_terms = derive_search_terms(feature_input, config.max_search_terms)
    matches = _search_feature_terms(repository, search_terms)
    candidates = _rank_candidates(tracked_files, search_terms, matches)
    candidates = _preserve_core_context(
        candidates,
        min(CORE_CONTEXT_FILE_LIMIT, config.max_selected_files),
    )

    evidence: list[RepositoryEvidence] = []
    skipped_file_count = 0
    total_text_characters = 0
    selection_limited = False

    for candidate_index, candidate in enumerate(candidates):
        if len(evidence) == config.max_selected_files:
            selection_limited = True
            break

        remaining_characters = (
            config.max_total_characters - total_text_characters
        )
        if remaining_characters == 0:
            selection_limited = True
            break

        read_limit = min(
            config.max_characters_per_file,
            remaining_characters,
        )
        resolved_path = _resolve_tracked_path(repository, candidate.file_path)
        if resolved_path is None:
            skipped_file_count += 1
            continue

        excerpt = _read_bounded_text(resolved_path, read_limit)
        if excerpt is None:
            skipped_file_count += 1
            continue

        text, truncated = excerpt
        evidence.append(
            RepositoryEvidence(
                id=f"repo-{len(evidence) + 1:03d}",
                file_path=candidate.file_path,
                selection_reasons=list(candidate.selection_reasons),
                matched_terms=list(candidate.matched_terms),
                text=text,
                truncated=truncated,
            )
        )
        total_text_characters += len(text)

        if (
            total_text_characters == config.max_total_characters
            and candidate_index < len(candidates) - 1
        ):
            selection_limited = True
            break

    return ContextBundle(
        repository_path=str(repository),
        commit_sha=commit_sha,
        branch=branch,
        working_tree_dirty=working_tree_dirty,
        feature_input=feature_input,
        search_terms=search_terms,
        configuration=config,
        tracked_file_count=len(tracked_files),
        candidate_file_count=len(candidates),
        selected_file_count=len(evidence),
        skipped_file_count=skipped_file_count,
        total_text_characters=total_text_characters,
        truncated_file_count=sum(item.truncated for item in evidence),
        selection_limited=selection_limited,
        evidence=evidence,
    )


def validate_repository_path(repository_path: str | Path) -> Path:
    """Resolve and validate a Git working-tree path without building context."""
    return _validate_repository(repository_path)


def validate_repository_evidence_ids(
    evidence_ids: Iterable[str],
    context: ContextBundle,
    *,
    supplemental_evidence_ids: Iterable[str] = (),
) -> list[RepositoryEvidence]:
    requested_ids = list(evidence_ids)
    evidence_by_id = {item.id: item for item in context.evidence}
    available_ids = set(evidence_by_id) | set(supplemental_evidence_ids)
    invalid_ids = [
        evidence_id
        for evidence_id in requested_ids
        if evidence_id not in available_ids
    ]

    if invalid_ids:
        invalid = ", ".join(invalid_ids)
        available = ", ".join(sorted(available_ids)) or "none"
        raise ContextBuilderError(
            f"Unknown repository evidence IDs: {invalid}. "
            f"Available repository evidence IDs: {available}."
        )

    return [
        evidence_by_id[evidence_id]
        for evidence_id in requested_ids
        if evidence_id in evidence_by_id
    ]


def _validate_repository(repository_path: str | Path) -> Path:
    supplied_path = Path(repository_path).expanduser().resolve()
    if not supplied_path.is_dir():
        raise ContextBuilderError(
            f"Repository path is not an existing directory: {supplied_path}"
        )

    inside_work_tree = _run_git(
        supplied_path,
        "rev-parse",
        "--is-inside-work-tree",
    ).strip()
    if inside_work_tree != "true":
        raise ContextBuilderError(
            f"Path is not inside a Git working tree: {supplied_path}"
        )

    root_output = _run_git(
        supplied_path,
        "rev-parse",
        "--show-toplevel",
    ).strip()
    repository = Path(root_output).resolve()
    if not repository.is_dir():
        raise ContextBuilderError(
            f"Git returned an invalid working-tree root: {root_output}"
        )
    return repository


def _run_git(
    repository: Path,
    *arguments: str,
    allowed_return_codes: tuple[int, ...] = (0,),
) -> str:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"

    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=environment,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as error:
        raise ContextBuilderError(
            "Git executable was not found. Install Git and make it available "
            "to the Context Builder process."
        ) from error

    if result.returncode not in allowed_return_codes:
        detail = result.stderr.strip() or result.stdout.strip()
        if not detail:
            detail = f"Git exited with code {result.returncode}."

        detail_lower = detail.casefold()
        if "dubious ownership" in detail_lower or "safe.directory" in detail_lower:
            raise ContextBuilderError(
                f"Git refused repository '{repository}' because of its "
                "ownership or trust configuration. Resolve repository "
                "ownership or Git safe.directory configuration in the caller. "
                f"Original Git error: {detail}"
            )
        if "not a git repository" in detail_lower:
            raise ContextBuilderError(
                f"Path is not inside a Git working tree: {repository}. "
                f"Original Git error: {detail}"
            )

        command = " ".join(arguments)
        raise ContextBuilderError(
            f"Git command failed for repository '{repository}' "
            f"(git {command}): {detail}"
        )

    return result.stdout


def _list_tracked_files(repository: Path) -> list[str]:
    output = _run_git(repository, "ls-files", "-z")
    paths = [path for path in output.split("\0") if path]
    return sorted(set(paths), key=lambda path: (path.casefold(), path))


def _search_feature_terms(
    repository: Path,
    search_terms: list[str],
) -> dict[str, set[str]]:
    matches: dict[str, set[str]] = {}

    for term in search_terms:
        arguments = ["grep", "-l", "-i", "-F", "-z"]
        for search_form in _TERM_SEARCH_FORMS.get(term, (term,)):
            arguments.extend(("-e", search_form))
        arguments.append("--")
        output = _run_git(
            repository,
            *arguments,
            allowed_return_codes=(0, 1),
        )
        for file_path in (path for path in output.split("\0") if path):
            matches.setdefault(file_path, set()).add(term)

    return matches


def _rank_candidates(
    tracked_files: list[str],
    search_terms: list[str],
    matches: dict[str, set[str]],
) -> list[_RankedCandidate]:
    term_order = {term: index for index, term in enumerate(search_terms)}
    candidates: list[_RankedCandidate] = []

    for file_path in tracked_files:
        if PurePosixPath(file_path).suffix.casefold() == ".meta":
            continue

        reasons, score = _categorise_file(file_path)
        matched_terms = tuple(
            sorted(matches.get(file_path, set()), key=term_order.__getitem__)
        )
        if matched_terms:
            reasons.append("feature term match")
            score += 2_000 + (100 * len(matched_terms))
            if len(matched_terms) > 1:
                reasons.append("multiple feature term matches")

        if not reasons:
            continue

        candidates.append(
            _RankedCandidate(
                file_path=file_path,
                selection_reasons=tuple(reasons),
                matched_terms=matched_terms,
                score=score,
            )
        )

    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate.score,
            candidate.file_path.casefold(),
            candidate.file_path,
        ),
    )


def _preserve_core_context(
    candidates: list[_RankedCandidate],
    core_file_limit: int,
) -> list[_RankedCandidate]:
    core_candidates = sorted(
        (
            candidate
            for candidate in candidates
            if _core_context_priority(candidate.file_path) > 0
        ),
        key=lambda candidate: (
            -_core_context_priority(candidate.file_path),
            -candidate.score,
            candidate.file_path.casefold(),
            candidate.file_path,
        ),
    )[:core_file_limit]
    explained_core_candidates = [
        replace(
            candidate,
            selection_reasons=(
                "reserved core context",
                *candidate.selection_reasons,
            ),
        )
        for candidate in core_candidates
    ]
    core_paths = {candidate.file_path for candidate in core_candidates}
    return explained_core_candidates + [
        candidate
        for candidate in candidates
        if candidate.file_path not in core_paths
    ]


def _core_context_priority(file_path: str) -> int:
    path = PurePosixPath(file_path)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    is_root_file = len(path.parts) == 1

    if name == "agents.md":
        return 700 if is_root_file else 650
    if name == "skill.md" or name.endswith("instructions.md"):
        return 600 if is_root_file else 550
    if is_root_file and name.startswith("readme"):
        return 500
    if is_root_file and (
        name in _MANIFEST_NAMES or suffix in _MANIFEST_SUFFIXES
    ):
        return 400
    if is_root_file and suffix in _CONFIG_SUFFIXES:
        return 300
    if suffix in _MANIFEST_SUFFIXES:
        return 200
    return 0


def _categorise_file(file_path: str) -> tuple[list[str], int]:
    path = PurePosixPath(file_path)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    reasons: list[str] = []
    score = 0

    is_root_file = len(path.parts) == 1
    is_readme = is_root_file and name.startswith("readme")
    is_agents = name == "agents.md"
    is_instruction = (
        is_agents
        or name == "skill.md"
        or name.endswith("instructions.md")
    )
    is_manifest = name in _MANIFEST_NAMES or suffix in _MANIFEST_SUFFIXES

    if is_readme or is_agents or is_manifest:
        reasons.append("exact filename candidate")
        score += 700
    if is_instruction:
        reasons.append("project instruction candidate")
        score += 800
    if is_root_file and (is_readme or is_agents or is_manifest):
        reasons.append("repository metadata candidate")
        score += 600

    path_parts = {part.casefold() for part in path.parts[:-1]}
    is_test = (
        "test" in path_parts
        or "tests" in path_parts
        or name.startswith("test_")
        or name.endswith("_test.py")
    )
    if is_test:
        reasons.append("test relevance")
        score += 400
    if suffix in _SOURCE_SUFFIXES:
        reasons.append("source relevance")
        score += 250
    if suffix in _CONFIG_SUFFIXES:
        reasons.append("config relevance")
        score += 350
    if suffix in _DOCUMENTATION_SUFFIXES:
        reasons.append("documentation relevance")
        score += 300

    return reasons, score


def _resolve_tracked_path(repository: Path, file_path: str) -> Path | None:
    relative_path = PurePosixPath(file_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None

    resolved_path = repository.joinpath(*relative_path.parts).resolve()
    try:
        resolved_path.relative_to(repository)
    except ValueError:
        return None

    if not resolved_path.is_file():
        return None
    return resolved_path


def _read_bounded_text(
    file_path: Path,
    character_limit: int,
) -> tuple[str, bool] | None:
    try:
        with file_path.open("rb") as binary_file:
            if b"\0" in binary_file.read(4_096):
                return None

        with file_path.open("r", encoding="utf-8", errors="strict") as text_file:
            content = text_file.read(character_limit + 1)
    except (OSError, UnicodeError):
        return None

    truncated = len(content) > character_limit
    return content[:character_limit], truncated
