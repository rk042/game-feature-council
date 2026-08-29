from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from council.models import RoleTelemetry, TokenUsage
from council.pricing import DEFAULT_PRICING_SNAPSHOT, estimate_cost
from council.progress import ProgressEvent, ProgressStatus


ROLE_LABELS = {
    "context": "Context Builder",
    "game_design": "Game Design",
    "technical": "Technical",
    "analytics": "Analytics",
    "scope_risk": "Scope / Risk",
    "producer": "Producer",
    "director": "Director",
    "generalist": "Generalist baseline",
    "artifacts": "Artifact writing",
}
COUNCIL_STAGES = (
    "game_design",
    "technical",
    "analytics",
    "scope_risk",
    "producer",
    "director",
)


@dataclass
class _StageState:
    status: str = "Waiting"
    started_at: float | None = None
    duration_ms: float | None = None


class ExecutionProgress:
    """Consumes runtime events and owns terminal-only presentation state."""

    def __init__(
        self,
        mode: str,
        *,
        print_fn: Callable[[str], None],
        console: Console | None = None,
        live: bool = False,
        verbose: bool = False,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._print = print_fn
        self._console = console
        self._live_enabled = live and console is not None
        self._verbose = verbose
        self._clock = clock
        self._started_at = clock()
        self._live: Live | None = None
        self._logs: list[str] = []
        self._telemetry: dict[str, RoleTelemetry] = {}
        active = ["context"]
        if mode in {"council", "both"}:
            active.extend(COUNCIL_STAGES)
        if mode in {"generalist", "both"}:
            active.append("generalist")
        active.append("artifacts")
        self._stages = {role: _StageState() for role in active}

    def start(self) -> None:
        if self._live_enabled:
            self._live = Live(
                self._render(),
                console=self._console,
                auto_refresh=False,
                transient=False,
            )
            self._live.start(refresh=True)

    def stop(self) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
            self._live.stop()
            self._live = None

    def refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)

    def complete_context(
        self,
        duration_ms: float,
        selected_files: int,
        characters: int,
    ) -> None:
        state = self._stages["context"]
        state.status = "Complete"
        state.duration_ms = duration_ms
        self._record_log(
            "Context",
            f"Selected {selected_files} files / {characters} chars",
        )

    def start_artifacts(self) -> None:
        self._set_started("artifacts")
        self._record_log("Artifacts", "Started")

    def complete_artifacts(self, duration_ms: float) -> None:
        state = self._stages["artifacts"]
        state.status = "Complete"
        state.duration_ms = duration_ms
        self._record_log("Artifacts", "Completed")

    def fail_remaining(self) -> None:
        for state in self._stages.values():
            if state.status == "Running":
                state.status = "Failed"
                if state.started_at is not None:
                    state.duration_ms = (
                        self._clock() - state.started_at
                    ) * 1_000
            elif state.status == "Waiting":
                state.status = "Blocked"
        self.refresh()

    def __call__(self, event: ProgressEvent) -> None:
        state = self._stages.get(event.role)
        if state is None:
            return
        if event.status == ProgressStatus.STARTED:
            self._set_started(event.role)
            detail = "Started"
        elif event.status == ProgressStatus.COMPLETED:
            state.status = "Complete"
            state.duration_ms = event.duration_ms
            detail = "Completed"
            if event.model is not None and event.usage is not None:
                self._telemetry[event.role] = RoleTelemetry(
                    model=event.model,
                    duration_ms=event.duration_ms or 0,
                    usage=event.usage,
                )
        else:
            state.status = "Failed"
            state.duration_ms = event.duration_ms
            detail = "Failed"

        if self._verbose and event.usage is not None:
            detail += (
                f"; {event.usage.total_tokens} tokens, "
                f"{event.usage.requests} request(s)"
            )
        self._record_log(ROLE_LABELS[event.role], detail)

    def _set_started(self, role: str) -> None:
        state = self._stages[role]
        state.status = "Running"
        state.started_at = self._clock()
        state.duration_ms = None

    def _record_log(self, role: str, message: str) -> None:
        elapsed = self._clock() - self._started_at
        line = f"{elapsed:7.1f}s  {role:<18} {message}"
        self._logs.append(line)
        if not self._live_enabled:
            self._print(line)
        self.refresh()

    def _render(self):
        table = Table(title="Game Feature Council - Running")
        table.add_column("Stage")
        table.add_column("Status")
        table.add_column("Time", justify="right")
        for role, state in self._stages.items():
            duration_ms = state.duration_ms
            if state.status == "Running" and state.started_at is not None:
                duration_ms = (self._clock() - state.started_at) * 1_000
            duration = (
                f"{duration_ms / 1_000:.1f}s"
                if duration_ms is not None
                else "-"
            )
            table.add_row(
                ROLE_LABELS[role],
                _status_text(state.status),
                duration,
            )

        usage = _aggregate_usage(self._telemetry)
        cost = estimate_cost(self._telemetry, DEFAULT_PRICING_SNAPSHOT)
        cost_text = (
            f"${cost.estimated_cost_usd:.6f}"
            if cost.estimated_cost_usd is not None
            else "unavailable"
        )
        summary = Text(
            f"Requests {usage.requests}  |  Tokens {usage.total_tokens}  |  "
            f"Estimated cost {cost_text}  |  "
            f"Elapsed {self._clock() - self._started_at:.1f}s"
        )
        logs = "\n".join(self._logs[-8:]) or "Waiting to start..."
        return Group(table, Panel(summary), Panel(logs, title="Operational log"))


def _aggregate_usage(roles: dict[str, RoleTelemetry]) -> TokenUsage:
    return TokenUsage(
        requests=sum(item.usage.requests for item in roles.values()),
        input_tokens=sum(item.usage.input_tokens for item in roles.values()),
        output_tokens=sum(item.usage.output_tokens for item in roles.values()),
        total_tokens=sum(item.usage.total_tokens for item in roles.values()),
    )


def _status_text(status: str) -> Text:
    styles = {
        "Waiting": "dim",
        "Running": "bold yellow",
        "Complete": "bold green",
        "Failed": "bold red",
        "Blocked": "red",
    }
    return Text(status, style=styles.get(status, ""))
