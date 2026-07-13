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
class EndpointInputDefinition(JsonModel):
    name: str
    label: str = ""
    kind: str = "string"
    required: bool = False
    default: str = ""
    option_source: str = ""
    options: list[str] = field(default_factory=list)
    min_value: str = ""
    max_value: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        self.label = str(self.label or self.name).strip()
        self.kind = str(self.kind or "string").strip().lower()
        aliases = {"text": "string", "number": "integer", "int": "integer", "decimal": "float"}
        self.kind = aliases.get(self.kind, self.kind)
        self.default = str(self.default or "")
        self.option_source = str(self.option_source or "").strip()
        if not self.name:
            raise ValidationError("Endpoint input name cannot be empty")
        if self.kind not in {"string", "integer", "float", "bool", "select"}:
            raise ValidationError(f"Unsupported endpoint input kind: {self.kind}")


@dataclass
class EndpointMatchRule(JsonModel):
    input_name: str
    operator: str = "equals"
    value: str = ""

    def __post_init__(self) -> None:
        self.input_name = str(self.input_name or "").strip()
        self.operator = str(self.operator or "equals").strip().lower()
        self.value = str(self.value if self.value is not None else "")
        if not self.input_name:
            raise ValidationError("Match rule input name cannot be empty")
        if self.operator not in {"equals", "not_equals", "exists", "missing", "contains"}:
            raise ValidationError(f"Unsupported endpoint match operator: {self.operator}")


@dataclass
class EndpointResponseDefinition(JsonModel):
    response_type: str = "execution"
    success_body: str = ""
    error_body: str = ""
    media_type: str = "application/json"

    def __post_init__(self) -> None:
        self.response_type = str(self.response_type or "execution").strip().lower()
        self.media_type = str(self.media_type or "application/json").strip()
        if self.response_type not in {"execution", "last_action_data", "static_json", "plain_text", "binary"}:
            raise ValidationError(f"Unsupported endpoint response type: {self.response_type}")


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
    inputs: list[EndpointInputDefinition] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    match_rules: list[EndpointMatchRule] = field(default_factory=list)
    behavior_mode: str = "actions"
    response: EndpointResponseDefinition = field(default_factory=EndpointResponseDefinition)

    def __post_init__(self) -> None:
        self.key = str(self.key or "").strip()
        self.name = str(self.name or "").strip()
        self.route = str(self.route or "").strip()
        if not self.key or not self.name or not self.route:
            raise ValidationError("Endpoint key, name, and route are required")
        if not self.route.startswith("/"):
            raise ValidationError(f"Endpoint route must start with /: {self.route}")
        self.aliases = [str(alias or "").strip() for alias in self.aliases if str(alias or "").strip()]
        for alias in self.aliases:
            if not alias.startswith("/"):
                raise ValidationError(f"Endpoint alias must start with /: {alias}")
        self.allowed_methods = [str(method or "").strip().upper() for method in self.allowed_methods if str(method or "").strip()]
        self.behavior_mode = str(self.behavior_mode or "actions").strip().lower()
        if self.behavior_mode not in {"actions", "read", "actions_then_read"}:
            raise ValidationError(f"Unsupported endpoint behavior mode: {self.behavior_mode}")


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
