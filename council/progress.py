from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from council.models import TokenUsage


class ProgressStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ProgressEvent:
    role: str
    status: ProgressStatus
    timestamp: datetime
    duration_ms: float | None = None
    model: str | None = None
    usage: TokenUsage | None = None
    message: str | None = None


ProgressListener = Callable[[ProgressEvent], None]


def make_progress_event(
    role: str,
    status: ProgressStatus,
    *,
    duration_ms: float | None = None,
    model: str | None = None,
    usage: TokenUsage | None = None,
    message: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        role=role,
        status=status,
        timestamp=datetime.now(timezone.utc),
        duration_ms=duration_ms,
        model=model,
        usage=usage,
        message=message,
    )
