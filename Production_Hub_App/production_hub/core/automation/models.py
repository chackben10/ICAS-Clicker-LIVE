from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from production_hub.core.config.models import JsonModel, ValidationError
from production_hub.core.endpoints.models import ActionDefinition


@dataclass
class AutomationDefinition(JsonModel):
    key: str
    name: str
    trigger: str
    enabled: bool = True
    interval_seconds: float = 0
    cooldown_seconds: float = 0
    debounce_seconds: float = 0
    conditions: list[dict[str, Any]] = field(default_factory=list)
    actions: list[ActionDefinition] = field(default_factory=list)
    repeated_error_disable_threshold: int = 5
    description: str = ""

    def __post_init__(self) -> None:
        self.key = str(self.key or "").strip()
        self.name = str(self.name or "").strip()
        self.trigger = str(self.trigger or "").strip()
        if not self.key or not self.name or not self.trigger:
            raise ValidationError("Automation key, name, and trigger are required")
        self.interval_seconds = max(0, float(self.interval_seconds))
        self.cooldown_seconds = max(0, float(self.cooldown_seconds))
        self.debounce_seconds = max(0, float(self.debounce_seconds))


@dataclass
class AutomationRunState(JsonModel):
    key: str
    enabled: bool = True
    last_execution_at: str = ""
    last_condition_result: str = ""
    last_action_result: str = ""
    last_error: str = ""
    run_count: int = 0
    consecutive_errors: int = 0

    def mark_success(self, result: str) -> None:
        self.last_execution_at = datetime.now(UTC).isoformat()
        self.last_action_result = result
        self.last_error = ""
        self.run_count += 1
        self.consecutive_errors = 0

    def mark_error(self, error: str) -> None:
        self.last_execution_at = datetime.now(UTC).isoformat()
        self.last_error = error
        self.run_count += 1
        self.consecutive_errors += 1

