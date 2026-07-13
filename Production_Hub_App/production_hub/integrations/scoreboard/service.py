from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from production_hub.core.config.models import ScoreboardConfig, ValidationError
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_DISABLED, STATUS_OFFLINE
from production_hub.integrations.base import IntegrationBase
from production_hub.integrations.scoreboard.models import ScoreRow, ScoreboardState
from production_hub.integrations.scoreboard.repository import ScoreboardRepository


class ScoreboardConflict(RuntimeError):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"Scoreboard revision conflict: expected {expected}, actual {actual}")


class ScoreboardDisabled(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Scoreboard Service is disabled")


class ScoreboardService(IntegrationBase):
    def __init__(self, repository: ScoreboardRepository, config: ScoreboardConfig | None = None) -> None:
        self.config = config or ScoreboardConfig()
        super().__init__("Scoreboard Service", self.config.enabled, "state/scoreboard.json")
        self.repository = repository

    def health(self) -> IntegrationHealth:
        self.enabled = self.config.enabled
        if not self.enabled:
            return IntegrationHealth(self.name, STATUS_DISABLED, self.target)
        if self.last_error:
            return IntegrationHealth(self.name, STATUS_OFFLINE, self.target, self.last_success_at, self.last_error)
        return IntegrationHealth(self.name, STATUS_CONNECTED, self.target, self.last_success_at, self.last_error)

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = enabled
        self.enabled = enabled
        if enabled:
            self.last_error = ""

    def require_enabled(self) -> None:
        self.enabled = self.config.enabled
        if not self.enabled:
            raise ScoreboardDisabled()

    def get_state(self) -> ScoreboardState:
        self.require_enabled()
        state = self.repository.load()
        self.mark_success()
        return state

    def update_state(
        self,
        payload: dict[str, Any],
        writer: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> ScoreboardState:
        self.require_enabled()
        with self.repository.locked():
            current = self.repository.load()
            if expected_revision is not None and expected_revision != current.revision:
                raise ScoreboardConflict(expected_revision, current.revision)

            incoming = ScoreboardState.from_legacy_payload(payload)
            incoming.validate_unique_rows()
            incoming.revision = current.revision + 1
            incoming.modified_at = datetime.now(UTC).isoformat()
            incoming.last_writer = writer or {}
            self.repository.save(incoming)
            self.mark_success()
            return incoming

    def add_row(self, name: str = "", score: int = 0) -> ScoreboardState:
        self.require_enabled()
        state = self.repository.load()
        next_id = f"row-{state.revision + 1}-{len(state.rows) + 1}"
        state.history.append([ScoreRow(row.id, row.name, row.score) for row in state.rows])
        state.history = state.history[-100:]
        state.rows.append(ScoreRow(next_id, name, score))
        state.revision += 1
        state.modified_at = datetime.now(UTC).isoformat()
        state.validate_unique_rows()
        self.repository.save(state)
        return state

    def undo(self) -> ScoreboardState:
        self.require_enabled()
        state = self.repository.load()
        if not state.history:
            raise ValidationError("No scoreboard history available")
        state.rows = state.history.pop()
        state.revision += 1
        state.modified_at = datetime.now(UTC).isoformat()
        self.repository.save(state)
        return state
