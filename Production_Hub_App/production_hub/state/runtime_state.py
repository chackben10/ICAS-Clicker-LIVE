from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from production_hub.core.config.models import JsonModel


@dataclass
class RequestRecord(JsonModel):
    timestamp: str
    method: str
    route: str
    status_code: int
    caller_ip: str = ""
    duration_ms: float = 0
    request_id: str = ""
    error: str = ""


@dataclass
class RuntimeState(JsonModel):
    schema_version: int = 1
    auto_show_enabled: bool = False
    current_obs_scene: str = ""
    current_propresenter_look: str = ""
    current_active_audio: str = ""
    current_profile: str = "Default Profile"
    endpoint_request_history: list[RequestRecord] = field(default_factory=list)
    automation_history: list[dict[str, Any]] = field(default_factory=list)
    health_state: dict[str, Any] = field(default_factory=dict)

    def add_request(self, record: RequestRecord, max_items: int = 500) -> None:
        self.endpoint_request_history.append(record)
        if len(self.endpoint_request_history) > max_items:
            self.endpoint_request_history = self.endpoint_request_history[-max_items:]

