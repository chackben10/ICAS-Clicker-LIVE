from __future__ import annotations

import time
from collections.abc import Iterable

from production_hub.core.config.models import AppConfig
from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, SystemHealth


class HealthMonitor:
    def __init__(self, config: AppConfig, started_at: float | None = None) -> None:
        self.config = config
        self.started_at = started_at or time.monotonic()
        self._integration_health: dict[str, IntegrationHealth] = {}

    def update(self, health: IntegrationHealth) -> None:
        self._integration_health[health.name] = health

    def integration_list(self) -> list[IntegrationHealth]:
        expected = [
            "ProPresenter",
            "OBS",
            "Panasonic AWP",
            "VISCA Bridge",
            "Scoreboard Service",
            "Remote API Server",
            "MIDI",
        ]
        return [self._integration_health[name] for name in expected if name in self._integration_health]

    def snapshot(
        self,
        endpoints: Iterable[object],
        automations: Iterable[object],
        recent_errors: int = 0,
        api_status: str = STATUS_CONNECTED,
    ) -> SystemHealth:
        return SystemHealth(
            app_running=True,
            active_profile=self.config.active_profile,
            api_status=api_status,
            api_target=self.config.api.base_url,
            uptime_seconds=round(time.monotonic() - self.started_at, 2),
            enabled_endpoints=sum(1 for item in endpoints if getattr(item, "enabled", True)),
            active_automations=sum(1 for item in automations if getattr(item, "enabled", True)),
            recent_errors=recent_errors,
            integrations=self.integration_list(),
        )

