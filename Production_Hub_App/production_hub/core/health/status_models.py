from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from production_hub.core.config.models import JsonModel


STATUS_CONNECTED = "Connected"
STATUS_OFFLINE = "Offline"
STATUS_WARNING = "Warning"
STATUS_DISABLED = "Disabled"
STATUS_RECONNECTING = "Reconnecting"


@dataclass
class IntegrationHealth(JsonModel):
    name: str
    status: str
    target: str = ""
    last_success_at: str = ""
    last_error: str = ""
    retry_state: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemHealth(JsonModel):
    app_running: bool
    active_profile: str
    api_status: str
    api_target: str
    uptime_seconds: float
    enabled_endpoints: int
    active_automations: int
    recent_errors: int
    integrations: list[IntegrationHealth]

