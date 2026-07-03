from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from production_hub.core.config.models import JsonModel, ValidationError


@dataclass
class ActionDefinition(JsonModel):
    action_type: str
    params: dict[str, Any] = field(default_factory=dict)
    delay_seconds: float = 0
    retries: int = 0
    retry_delay_seconds: float = 0.25
    condition: str = ""

    def __post_init__(self) -> None:
        self.action_type = str(self.action_type or "").strip()
        if not self.action_type:
            raise ValidationError("action_type cannot be empty")
        self.delay_seconds = max(0, float(self.delay_seconds))
        self.retries = max(0, int(self.retries))
        self.retry_delay_seconds = max(0, float(self.retry_delay_seconds))


@dataclass
class EndpointDefinition(JsonModel):
    key: str
    name: str
    route: str
    actions: list[ActionDefinition]
    enabled: bool = True
    dangerous: bool = False
    description: str = ""
    allowed_methods: list[str] = field(default_factory=lambda: ["GET", "POST"])

    def __post_init__(self) -> None:
        self.key = str(self.key or "").strip()
        self.name = str(self.name or "").strip()
        self.route = str(self.route or "").strip()
        if not self.key or not self.name or not self.route:
            raise ValidationError("Endpoint key, name, and route are required")
        if not self.route.startswith("/"):
            raise ValidationError(f"Endpoint route must start with /: {self.route}")


@dataclass
class ActionResult(JsonModel):
    action_type: str
    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class EndpointExecutionResult(JsonModel):
    endpoint_key: str
    ok: bool
    request_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = ""
    action_results: list[ActionResult] = field(default_factory=list)
    error: str = ""

