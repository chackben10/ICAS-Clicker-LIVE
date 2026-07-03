from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from production_hub.core.health.status_models import IntegrationHealth, STATUS_CONNECTED, STATUS_DISABLED, STATUS_OFFLINE


@dataclass
class IntegrationBase:
    name: str
    enabled: bool = True
    target: str = ""
    last_success_at: str = ""
    last_error: str = ""

    def mark_success(self) -> None:
        self.last_success_at = datetime.now(UTC).isoformat()
        self.last_error = ""

    def mark_error(self, error: str) -> None:
        self.last_error = str(error)

    def health(self) -> IntegrationHealth:
        if not self.enabled:
            status = STATUS_DISABLED
        elif self.last_error:
            status = STATUS_OFFLINE
        else:
            status = STATUS_CONNECTED if self.last_success_at else STATUS_OFFLINE
        return IntegrationHealth(self.name, status, self.target, self.last_success_at, self.last_error)

